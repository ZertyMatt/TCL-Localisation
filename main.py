from flask import Flask, jsonify, Response, request
import json
import gzip
from flask_cors import CORS
import requests
import os
import re
import io
import csv
import zipfile
import unicodedata
from collections import Counter
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    PARIS_TZ = ZoneInfo('Europe/Paris')
except Exception:
    PARIS_TZ = None   # pas de fuseau dispo (Windows sans tzdata) : on prend l'heure de la machine
import threading
import time
import gc
import ctypes
from dotenv import load_dotenv

load_dotenv()  # Charge automatiquement le fichier .env

app = Flask(__name__)
CORS(app)

# On va chercher les secrets dans le système au lieu de les écrire ici
USER = os.environ.get('GRANDLYON_USER')
PASS = os.environ.get('GRANDLYON_PASS')

# --- Session HTTP réutilisée (connexion gardée ouverte = requêtes plus rapides) ---
http = requests.Session()
http.auth = (USER, PASS)
TIMEOUT_DATA = (5, 60)   # (connexion, lecture) : tclarret/all.json est lourd, Grand Lyon peut être lent
TIMEOUT_BUS = (5, 20)
RETRIES = 3

NAMES = ['parkings', 'parkings_tr', 'traffic', 'accessibility', 'velov', 'stops', 'lines', 'covoit']
fetch_locks = {n: threading.Lock() for n in NAMES}   # une seule requête à la fois par jeu de données
loaded_events = {n: threading.Event() for n in NAMES}  # levé dès qu'on a des données
bus_lock = threading.Lock()

# --- Cache pour stocker les données ---
cache = {
    'parkings': None,
    'traffic': None,
    'accessibility': None,
    'velov': None,
    'stops': None,
    'lines': None,   # tracés des lignes : { LIGNE: octets JSON }
    'covoit': None,  # En Covoit' Lignes : {stops, lines}
    'parkings_tr': None,  # places libres en temps réel des parcs relais
    'last_update': {}
}

# --- Statut du chargement initial ---
initial_load_status = {
    'parkings': False,
    'traffic': False,
    'accessibility': False,
    'velov': False,
    'stops': False,
    'lines': False,
    'covoit': False,
    'parkings_tr': False
}

# --- Champs réellement utilisés par la page : on n'envoie rien d'autre (réponses plus légères) ---
KEEP_FIELDS = {
    'stops': ['id', 'nom', 'code_station', 'lat', 'lon', 'desserte', 'commune', 'zone', 'pmr', 'adresse'],
    'velov': ['name', 'address', 'number', 'commune', 'lat', 'lng', 'available_bikes',
              'available_bike_stands', 'bike_stands', 'banking', 'status', 'last_update'],
    'traffic': ['titre', 'message', 'type', 'cause', 'ligne_cli', 'ligne_com', 'mode'],
    'accessibility': ['type_equipement', 'nom_station', 'cause', 'debut_indispo', 'fin_indispo', 'equipement'],
}

# --- Réponses JSON fabriquées une seule fois puis compressées (gzip) ---
_packed = {}

def fast_json(key, data):
    entry = _packed.get(key)
    if entry is None or entry[0] is not data:
        raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        entry = (data, raw, gzip.compress(raw, 5))
        _packed[key] = entry
    use_gzip = 'gzip' in request.headers.get('Accept-Encoding', '')
    resp = Response(entry[2] if use_gzip else entry[1], mimetype='application/json')
    if use_gzip:
        resp.headers['Content-Encoding'] = 'gzip'
    resp.headers['Vary'] = 'Accept-Encoding'
    return resp

def trim_memory():
    """Après un gros chargement : ramasse les déchets et rend la mémoire libre au système (sinon Render la compte toujours)"""
    gc.collect()
    try:
        ctypes.CDLL('libc.so.6').malloc_trim(0)   # Linux (Render) ; ignoré ailleurs
    except Exception:
        pass

# Petit cache de 5 s pour les bus (plusieurs onglets ouverts = une seule requête vers Grand Lyon)
bus_cache = {'t': 0, 'data': None}

# Couleurs par ligne (remplace l'extraction automatique)
LINE_COLORS = {
    # 83c491
    'C6': '#83c491', 'C202': '#83c491', '38': '#83c491', '76': '#83c491', '112': '#83c491', '141': '#83c491', '143': '#83c491', '147': '#83c491',
    '12': '#83c491', 'S6': '#83c491', 'S8': '#83c491', 'Zi3': '#83c491','4252': '#83c491',
    'T4': '#662483', 'C8': '#662483', '31': '#662483', '66': '#662483', '69': '#662483', '78': '#662483', '115': '#662483', '161': '#662483', '236': '#662483',
    'GE6': '#662483', '15': '#662483',
    'T5': '#ec6608', 'C5': '#ec6608', 'C205': '#ec6608', '40': '#ec6608',
    '7': '#ec6608', '11': '#ec6608', '23': '#ec6608', '26': '#ec6608',
    'GE4': '#ec6608', '155': '#ec6608', '211': '#ec6608', '239': '#ec6608',
    'N100': '#ec6608','6029': '#ec6608',
    'T6': '#f191a3', 'C14': '#f191a3', '82': '#f191a3', '86': '#f191a3', '104A': '#f191a3', '109': '#f191a3', '219': '#f191a3', '241': '#f191a3',
    'S4A': '#f191a3', '9': '#f191a3',
    'TB11': '#fdc300', 'C25': '#fdc300', 'C200': '#fdc300', '45': '#fdc300',
    '137': '#fdc300', 'S15': '#fdc300', '144': '#fdc300', 'S9': '#fdc300',
    '231': '#fdc300', '232': '#fdc300', '247': '#fdc300', '32': '#fdc300','5001': '#fdc300',
    'C2': '#e50069', '57': '#e50069', '72': '#e50069', '125': '#e50069', '25': '#e50069',
    'C20': '#836c77', '79': '#836c77', '102': '#836c77',
    '114': '#836c77', '2': '#836c77', '14': '#836c77', 'N187': '#836c77',
    'C16': '#c57e65', '65': '#c57e65',
    'T3': '#00a3a6', 'NAVI1': '#00a3a6', 'C18': '#00a3a6', '54': '#00a3a6', '90': '#00a3a6', '124': '#00a3a6', '134': '#00a3a6', '159': '#00a3a6',
    'N180': '#00a3a6', '24': '#00a3a6', 'S10': '#00a3a6', '7601': '#00a3a6',
    'T2': '#6ba230', 'C27': '#6ba230', '32': '#6ba230', '34': '#6ba230', '105': '#6ba230', '5': '#6ba230', '106EX': '#6ba230', '6E': '#6ba230', '135': '#6ba230',
    '142': '#6ba230', '156': '#6ba230', 'S7': '#6ba230','S11': '#6ba230', 'C3': '#6ba230', 'C11': '#6ba230', '240': '#6ba230', '2004': '#6ba230',
    '33': '#31287d', '106': '#31287d', '136': '#31287d',
    '6': '#31287d', 'S14': '#31287d',
    'C15': '#d3d800', '44': '#d3d800', '61': '#d3d800', '98': '#d3d800', '129': '#d3d800', '220': '#d3d800', 'N181': '#d3d800',
    'N81': '#d3d800', '3085': '#d3d800', 
    'F2': '#006f9e', '49': '#006f9e', '85': '#006f9e', '118': '#006f9e', '148': '#006f9e', '243': '#006f9e',
    '18': '#006f9e', '4133': '#006f9e',
    '36': '#6e8997', '87': '#6e8997', '110EX': '#6e8997', '218': '#6e8997', '222': '#6e8997', '150': '#6e8997', '164': '#6e8997',
    'C7': '#6e8997', '10E': '#6e8997', 'Zi8': '#6e8997','5003': '#6e8997',
    'C9': '#c88817', '50': '#c88817', '119': '#c88817', '88': '#c88817', '149': '#c88817', '151': '#c88817',
    '19': '#c88817',  'Zi5': '#c88817','6025': '#c88817',
    'TB12': '#80682e', 'C22EX': '#80682e', '46': '#80682e', '84': '#80682e', '117': '#80682e', '128': '#80682e', 'N185': '#80682e',
    '17': '#80682e',  'N20': '#80682e', 'C22E': '#80682e',
    'C23': '#009e3d', '52': '#009e3d', '55': '#009e3d', '89': '#009e3d', '97': '#009e3d', '104B': '#009e3d', '122': '#009e3d', '139': '#009e3d', '140': '#009e3d', '153': '#009e3d', '157': '#009e3d',  '245': '#009e3d',
    'S4B': '#009e3d',  '22': '#009e3d', 'S5': '#009e3d', 'S3': '#009e3d', '6027': '#009e3d', '3086': '#009e3d', 
    'C20EX': '#f59c00', 'C203': '#f59c00', '60': '#f59c00', 'BR60': '#f59c00', '81': '#f59c00', '116': '#f59c00', '120': '#f59c00', '146': '#f59c00',
    'C20E': '#f59c00',  '203': '#f59c00', '16': '#f59c00', '20': '#f59c00', 'Zi2': '#f59c00', 
    'C22': '#bca3ce', '35': '#bca3ce', '70': '#bca3ce', '108': '#bca3ce', '138': '#bca3ce', '154': '#bca3ce',
    '8': '#bca3ce',  'S2': '#bca3ce', '6028': '#bca3ce',
    '95': '#3f4e55', '98EX': '#3f4e55','C17': '#3f4e55',
    '98E': '#3f4e55',
    'C24': '#00336a', '64': '#00336a', '71': '#00336a', '103': '#00336a', 'N183': '#00336a', 
    '3': '#00336a', 'N83': '#00336a',
    'C10': '#cf7eae', '43': '#cf7eae', '80': '#cf7eae', '145': '#cf7eae',
    'Zi1': '#cf7eae',
    'C12': '#0099bc', 'C21': '#0099bc', '63': '#0099bc', 'N180': '#0099bc',
    'T1': '#004f9f', 'C1': '#004f9f', '39': '#004f9f', '77': '#004f9f', '133': '#004f9f',
    'S1': '#004f9f',
    'C26': '#2699d6', 'C204': '#2699d6', '68': '#2699d6', '99': '#2699d6', '130': '#2699d6', '212': '#2699d6', '214': '#2699d6', '237': '#2699d6', '248': '#2699d6', '249': '#2699d6', '285': '#2699d6',
    '5002': '#2699d6', '15E': '#2699d6', '3083': '#2699d6',
    'C201': '#0075bf', '37': '#0075bf', '62': '#0075bf', '110': '#0075bf', '152': '#0075bf',
    '10': '#0075bf', '6026': '#0075bf',
    'C2': '#e50069', '41': '#e50069', '93': '#e50069', '121': '#e50069', '165': '#e50069', '265': '#e50069',
    'C19': '#e50069', '21': '#e50069','3084': '#e50069',
    'T7': '#992358', '59': '#992358', 'F2': '#992358', '96': '#992358', '127': '#992358', '230': '#992358',
    '27': '#992358',
    '89EX': '#9f9825', '52EX': '#9f9825', '158': '#9f9825',
    '89D': '#9f9825', '52E': '#9f9825',
    '67': '#c20344', '216': '#c20344', 'C13': '#c20344','N195': '#c20344',
    'BGS1': '#c20344',
}

# --- Fonctions de chargement des données avec cache ---
def get_json_retry(name, url, timeout=TIMEOUT_DATA, tries=None):
    """GET avec nouvelles tentatives (Grand Lyon répond souvent en timeout). Renvoie le JSON ou None."""
    n = tries or RETRIES
    for attempt in range(1, n + 1):
        try:
            r = http.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            print(f"✗ Erreur {name}: HTTP {r.status_code} (essai {attempt}/{n})")
        except Exception as e:
            print(f"✗ Erreur {name}: {str(e)} (essai {attempt}/{n})")
        if attempt < n:
            time.sleep(2 * attempt)
    return None

# --- Parcs relais en temps réel (Grand Lyon : tclparcrelaistr) : places libres, état, heure de mise à jour ---
PARKINGS_TR_URL = ("https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.tclparcrelaistr/all.json"
                   "?maxfeatures=-1&start=1&filename=parcs-relais-reseau-transports-commun-lyonnais-disponibilites-temps-reel&sortby=gid")
# Noms de champs possibles (le jeu peut les nommer différemment selon les versions)
PR_LIBRES_KEYS = ('nb_tot_place_dispo', 'nb_places_libres', 'places_libres', 'nbplaceslibres', 'nb_places_disponibles', 'places_disponibles',
                  'disponibilite', 'disponible', 'dispo', 'libres', 'nb_libres', 'nb_places_dispo')
PR_OCCUPEES_KEYS = ('nb_voitures', 'nb_places_occupees', 'places_occupees', 'occupees', 'occupation')
PR_ETAT_KEYS = ('etat', 'statut', 'status', 'etat_parc')

def _pr_num(v):
    try:
        return int(float(str(v).replace(',', '.')))
    except (ValueError, TypeError):
        return None

def _pr_first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ''):
            return d[k]
    return None

def clean_parking_tr(v):
    d = {str(k).lower(): x for k, x in v.items()}
    cap = _pr_num(_pr_first(d, ('capacite', 'capacity', 'nb_places', 'places')))
    libres = _pr_num(_pr_first(d, PR_LIBRES_KEYS))
    if libres is None and cap is not None:
        occ = _pr_num(_pr_first(d, PR_OCCUPEES_KEYS))
        if occ is not None:
            libres = max(cap - occ, 0)
    return {
        'id': None if d.get('id') is None else str(d.get('id')),
        'nom': d.get('nom') or d.get('libelle'),
        'capacite': cap,
        'libres': libres,
        'etat': _pr_first(d, PR_ETAT_KEYS),
        'maj': d.get('last_update') or d.get('last_update_fme') or d.get('derniere_maj'),   # last_update = heure réelle de la mesure (last_update_fme = heure du transfert)
    }

def slim_item(v, keep):
    """Garde les champs utiles ; pour les Vélo'v, extrait aussi le détail mécaniques / électriques."""
    out = {k: v[k] for k in keep if k in v}
    av = ((v.get('total_stands') or {}).get('availabilities')) or {}
    if av:
        out['mechanical_bikes'] = av.get('mechanicalBikes', 0)
        out['electrical_bikes'] = av.get('electricalBikes', 0)
    return out

def fetch_with_cache(endpoint_name, url, fetch_func=None):
    """Charge et met en cache un jeu de données. Si un chargement est déjà en cours, on ne refait pas la requête."""
    lock = fetch_locks[endpoint_name]
    if not lock.acquire(blocking=False):
        return False
    try:
        print(f"Chargement de {endpoint_name}...")
        data = get_json_retry(endpoint_name, url)
        if data is None:
            return False   # on garde l'ancien cache s'il y en a un
        if endpoint_name == 'parkings_tr' and isinstance(data, dict) and isinstance(data.get('values'), list):
            if cache['parkings_tr'] is None and data['values']:
                print("ℹ parcs relais temps réel — champs reçus :", sorted(data['values'][0].keys()))
            data['values'] = [clean_parking_tr(v) for v in data['values']]
        keep = KEEP_FIELDS.get(endpoint_name)
        if keep and isinstance(data, dict) and isinstance(data.get('values'), list):
            data['values'] = [slim_item(v, keep) for v in data['values']]
        cache[endpoint_name] = data
        cache['last_update'][endpoint_name] = time.time()
        initial_load_status[endpoint_name] = True
        loaded_events[endpoint_name].set()
        print(f"✓ {endpoint_name} chargé avec succès")
        return True
    finally:
        lock.release()

def fetch_parkings():
    url_geo = (
        "https://data.grandlyon.com/geoserver/sytral/ows"
        "?SERVICE=WFS&VERSION=2.0.0&request=GetFeature"
        "&typename=sytral:tcl_sytral.tclparcrelaisst"
        "&outputFormat=application/json&SRSNAME=EPSG:4171&sortBy=gid"
    )
    if not fetch_locks['parkings'].acquire(blocking=False):
        return None
    try:
        raw_geo = get_json_retry('parkings', url_geo, (5, 30))
        if raw_geo is None:
            return None
        parkings = []

        for feature in raw_geo.get('features', []):
            props = feature.get('properties', {})
            coords = feature.get('geometry', {}).get('coordinates', [])

            if coords and isinstance(coords[0], list):
                coords = coords[0]

            if len(coords) < 2:
                continue

            lon, lat = coords[0], coords[1]

            if not (45.0 < lat < 46.5 and 4.0 < lon < 6.0):
                continue

            parkings.append({
                "id": props.get('id'),
                "nom": props.get('nom') or props.get('libelle') or "P+R",
                "capacite": props.get('capacite') or "?",
                "place_handi": props.get('place_handi') or 0,
                "horaires": props.get('horaires') or "Horaires non disponibles",
                "p_surv": props.get('p_surv', False),
                "lat": lat,
                "lon": lon,
                "last_update": props.get('last_update')
            })

        cache['parkings'] = parkings
        cache['last_update']['parkings'] = time.time()
        initial_load_status['parkings'] = True
        loaded_events['parkings'].set()
        print(f"✓ {len(parkings)} parkings chargés")
        return parkings
    except Exception as e:
        print(f"✗ Erreur chargement parkings: {str(e)}")
        return None
    finally:
        fetch_locks['parkings'].release()

# --- Tracés des lignes : géométrie (WFS) + sens / origine / destination (datapusher), reliés par code_trace ---
LINE_SOURCES = [   # (famille, datapusher, couche WFS)
    ('bus',  'tcllignebus_3',  'tcllignebus_2_0_0'),
    ('tram', 'tcllignetram_3', 'tcllignetram_2_0_0'),
    ('mf',   'tcllignemf_3',   'tcllignemf_2_0_0'),   # métro + funiculaire
]
SENS_CODE = {'aller': 'A', 'retour': 'R'}

def flatten_line(geom):
    """LineString / MultiLineString -> liste de [lat, lon] (5 décimales ≈ 1 m)"""
    if not geom:
        return []
    kind, coords = geom.get('type'), geom.get('coordinates') or []
    parts = [coords] if kind == 'LineString' else coords if kind == 'MultiLineString' else []
    pts = []
    for part in parts:
        for p in part:
            if len(p) >= 2:
                pt = [round(p[1], 5), round(p[0], 5)]
                if not pts or pts[-1] != pt:
                    pts.append(pt)
    return pts

lines_bytes = {}                # LIGNE -> octets JSON, accumulé famille par famille (plus d'objets Python gardés en mémoire)
lines_data_lock = threading.Lock()

def load_line_family(kind, meta_name, wfs_name):
    """Charge une famille (bus, tram, métro) et la publie tout de suite, sans attendre les autres."""
    geo = get_json_retry(f'lines-{kind}',
        "https://data.grandlyon.com/geoserver/sytral/ows?SERVICE=WFS&VERSION=2.0.0&request=GetFeature"
        f"&typename=sytral:tcl_sytral.{wfs_name}&outputFormat=application/json&SRSNAME=EPSG:4171",
        (5, 180))
    if geo is None:
        print(f"✗ tracés {kind} indisponibles")
        return
    meta = get_json_retry(f'lines-{kind}-meta',
        f"https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.{meta_name}/all.json?maxfeatures=-1&start=1")
    info = {}
    for v in (meta or {}).get('values', []):
        info[v.get('code_trace')] = v
    local = {}
    for f in geo.get('features', []):
        props = f.get('properties') or {}
        code = props.get('code_trace')
        m = info.get(code, {})
        ligne = props.get('ligne') or m.get('ligne')
        pts = flatten_line(f.get('geometry'))
        if not ligne or len(pts) < 2:
            continue
        local.setdefault(str(ligne).upper(), []).append({
            't': code,
            's': SENS_CODE.get(str(m.get('sens', '')).lower()),   # A = aller, R = retour (None si inconnu)
            'o': m.get('origine'),
            'd': m.get('destination'),
            'c': m.get('couleur_hex'),
            'k': props.get('type_trace') or m.get('type_trace'),
            'g': pts,
        })
    if not local:
        print(f"✗ tracés {kind} : aucune géométrie exploitable")
        return
    packed = {ligne: json.dumps({"values": traces}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
              for ligne, traces in local.items()}
    del geo, meta, info, local   # gros objets : libérés tout de suite
    with lines_data_lock:
        lines_bytes.update(packed)
        cache['lines'] = dict(lines_bytes)
        cache['last_update']['lines'] = time.time()
        initial_load_status['lines'] = True
        loaded_events['lines'].set()
    print(f"✓ tracés {kind} chargés ({len(packed)} lignes)")
    trim_memory()

def fetch_lines():
    lock = fetch_locks['lines']
    if not lock.acquire(blocking=False):
        return False
    try:
        print("Chargement de lines (tracés)...")
        for src in LINE_SOURCES:   # l'une après l'autre : en parallèle, les 3 gros fichiers WFS faisaient exploser la mémoire
            try:
                load_line_family(*src)
            except Exception as e:
                print(f"✗ Erreur tracés {src[0]}: {e}")
        return cache['lines'] is not None
    except Exception as e:
        print(f"✗ Erreur lines: {e}")
        return False
    finally:
        lock.release()

# --- En Covoit' Lignes : arrêts + lignes (datapusher), tracés (WFS, sinon GTFS) et horaires théoriques (GTFS) ---
DP_URL = "https://data.grandlyon.com/fr/datapusher/ws/rdata/"
COVOIT_ARRETS_URL = DP_URL + "tcl_sytral.covoiturage_arret/all.json?maxfeatures=-1&start=1&sortby=gid"
COVOIT_LIGNES_URL = DP_URL + "tcl_sytral.covoiturage_ligne/all.json?maxfeatures=-1&start=1&sortby=gid"
COVOIT_WFS_URL = ("https://data.grandlyon.com/geoserver/sytral/ows?SERVICE=WFS&VERSION=2.0.0&request=GetFeature"
                  "&typename=sytral:tcl_sytral.covoiturage_ligne&outputFormat=application/json&SRSNAME=EPSG:4171")
COVOIT_GTFS_URL = "https://download.data.grandlyon.com/files/rdata/tcl_sytral.covoituragetheorique/gtfs_covoit.zip"
# Sources possibles pour la géométrie des lignes (la première qui donne des tracés est retenue)
COVOIT_GEO_SOURCES = [
    ("WFS geoserver", COVOIT_WFS_URL),
    ("WFS geoserver sans préfixe", COVOIT_WFS_URL.replace("typename=sytral:tcl_sytral.", "typename=tcl_sytral.")),
    ("WFS download", "https://download.data.grandlyon.com/wfs/rdata?SERVICE=WFS&VERSION=2.0.0&request=GetFeature"
                     "&typename=tcl_sytral:tcl_sytral.covoiturage_ligne&outputFormat=application/json;%20subtype=geojson&SRSNAME=EPSG:4171"),
    ("GeoJSON data.gouv.fr", "https://www.data.gouv.fr/api/1/datasets/r/b7090255-c827-4257-a242-84174f905881"),
]

def covoit_tokens(num):
    """numligne d'un arrêt : "4", "Lane-1", "5_6", "Lane-1_3" -> liste des lignes desservies"""
    return re.findall(r'Lane-\d+|\d+', str(num or ''))

def covoit_key(text):
    """Nom de ligne (route GTFS, etc.) -> numligne du datapusher"""
    t = str(text or '')
    if re.search(r'lane', t, re.I):
        return 'Lane-1'
    m = re.search(r'\d+', t)
    return m.group(0) if m else None

def norm_name(text):
    t = unicodedata.normalize('NFD', str(text or '').lower())
    return re.sub(r'[^a-z0-9]', '', ''.join(c for c in t if unicodedata.category(c) != 'Mn'))

def gtfs_time(t):
    parts = str(t or '').strip().split(':')
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    return f"{int(parts[0]):02d}:{parts[1][:2]}"

def gtfs_secs(t):
    parts = str(t or '').strip().split(':')
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + (int(parts[2]) if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return None

def clean_dest(text):
    """« En Covoit' Lignes - Villeurbanne - Laurent Bonnevay / Trévoux - Passerelle » -> « Trévoux - Passerelle » (destination après le « / »)"""
    t = str(text or '').split('/')[-1].strip()
    return re.sub(r"^En Covoit['’] ?Lignes\s*-\s*", '', t).strip()

def read_gtfs_covoit(arrets):
    """GTFS théorique : horaires par arrêt GTFS, formes (shapes) et séquence d'arrêts par ligne.
    Le numéro de ligne vient du nom de la route, sinon (ou s'il ne colle pas) des arrêts En Covoit' voisins de ses arrêts."""
    r = http.get(COVOIT_GTFS_URL, timeout=(5, 60))
    if r.status_code in (401, 403):
        r = requests.get(COVOIT_GTFS_URL, timeout=(5, 60))   # certains fichiers sont ouverts : on réessaie sans identifiants
    if r.status_code != 200:
        raise RuntimeError(f"GTFS covoiturage : HTTP {r.status_code}")
    z = zipfile.ZipFile(io.BytesIO(r.content))

    def rows(name):
        if name not in z.namelist():
            return
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))

    routes = {x['route_id']: x for x in rows('routes.txt')}
    stops = {x['stop_id']: x for x in rows('stops.txt')}
    trips = {x['trip_id']: x for x in rows('trips.txt')}
    cal = {x['service_id']: x for x in rows('calendar.txt')}
    day_cols = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

    def days_of(trip):
        c = cal.get(trip.get('service_id'))
        return ''.join('1' if c and str(c.get(d, '0')).strip() == '1' else '0' for d in day_cols) if c else '1111111'

    # arrêt GTFS -> arrêt En Covoit' le plus proche (150 m)
    near = {}
    for sid, gs in stops.items():
        try:
            la, lo = float(gs['stop_lat']), float(gs['stop_lon'])
        except (ValueError, KeyError):
            continue
        best, bd = None, 150.0
        for st in arrets:
            d = (((la - st['lat']) * 111320) ** 2 + ((lo - st['lon']) * 111320 * 0.7) ** 2) ** 0.5
            if d < bd:
                best, bd = st, d
        if best:
            near[sid] = best

    raw_times, trip_stops, route_stops = [], {}, {}
    for st in rows('stop_times.txt'):
        trip = trips.get(st.get('trip_id'))
        secs = gtfs_secs(st.get('departure_time') or st.get('arrival_time'))
        if not trip or secs is None:
            continue
        rid, sid, tid = trip.get('route_id'), st.get('stop_id'), st.get('trip_id')
        try:
            seq_n = int(st.get('stop_sequence') or 0)
        except ValueError:
            seq_n = 0
        raw_times.append((sid, rid, tid, secs, seq_n, days_of(trip), clean_dest(trip.get('trip_headsign') or routes.get(rid, {}).get('route_long_name'))))
        route_stops.setdefault(rid, set()).add(sid)
        trip_stops.setdefault(tid, []).append((seq_n, sid))

    # GTFS « à fréquence » : les heures des trajets sont relatives, les vrais passages viennent de frequencies.txt
    freq = {}
    for fr in rows('frequencies.txt'):
        a, b = gtfs_secs(fr.get('start_time')), gtfs_secs(fr.get('end_time'))
        try:
            hw = int(float(fr.get('headway_secs') or 0))
        except ValueError:
            hw = 0
        if a is not None and b is not None and hw >= 60:
            freq.setdefault(fr.get('trip_id'), []).append((a, b, hw))
    trip_last, trip_base = {}, {}
    for sid, rid, tid, secs, seq_n, d, h in raw_times:
        if seq_n >= trip_last.get(tid, -1):
            trip_last[tid] = seq_n
        if tid not in trip_base or seq_n < trip_base[tid][0]:
            trip_base[tid] = (seq_n, secs)
    print(f"    GTFS : {len(freq)} trajets à fréquence (frequencies.txt)")

    route_key = {}
    for rid, route in routes.items():
        k = covoit_key(route.get('route_short_name')) or covoit_key(route.get('route_long_name'))
        votes = Counter(tok for sid in route_stops.get(rid, ()) if sid in near for tok in near[sid]['l'])
        if votes and (k is None or k not in votes):
            k = votes.most_common(1)[0][0]   # le nom ne colle pas : on se fie aux arrêts voisins
        route_key[rid] = k
        print(f"    GTFS route {route.get('route_short_name')!r} / {route.get('route_long_name')!r} -> ligne {k} ({len(route_stops.get(rid, ()))} arrêts)")
    print(f"    GTFS : {len(routes)} routes, {len(trips)} trajets, {len(stops)} arrêts, {sum(1 for sid in stops if sid in near)} arrêts reliés à un arrêt En Covoit'")

    groups = {}   # (arrêt GTFS, ligne, jours, destination) -> heures "HH:MM"
    def add_time(g, x):
        if 0 <= x < 86400:
            groups.setdefault(g, set()).add(f"{x // 3600:02d}:{x % 3600 // 60:02d}")
    for sid, rid, tid, secs, seq_n, d, h in raw_times:
        key = route_key.get(rid)
        if not key:
            continue
        if seq_n == trip_last.get(tid) and len(trip_stops.get(tid, ())) > 1:
            continue   # dernier arrêt du trajet : on y arrive, on n'en part pas
        g = (sid, key, d, h)
        if tid in freq:
            off = secs - trip_base[tid][1]
            for a, b, hw in freq[tid]:
                x = a
                while x < b:
                    add_time(g, x + off)
                    x += hw
        else:
            add_time(g, secs)
    times = {}
    for (sid, key, d, h), ts in groups.items():
        times.setdefault(sid, []).append({'l': key, 'd': d, 'h': h, 't': sorted(ts)})

    shape_pts = {}
    for sp in rows('shapes.txt'):
        try:
            shape_pts.setdefault(sp['shape_id'], []).append((int(float(sp['shape_pt_sequence'])), round(float(sp['shape_pt_lat']), 5), round(float(sp['shape_pt_lon']), 5)))
        except (ValueError, KeyError):
            pass
    shapes, seen = {}, set()
    for tid, trip in trips.items():
        key, sid = route_key.get(trip.get('route_id')), trip.get('shape_id')
        if key and sid in shape_pts and (key, sid) not in seen:
            seen.add((key, sid))
            shapes.setdefault(key, []).append([[a, b] for _, a, b in sorted(shape_pts[sid])])

    seq = {}   # repli : la plus longue suite d'arrêts d'un trajet de la ligne, à défaut de formes
    for tid, items in trip_stops.items():
        key = route_key.get(trips[tid].get('route_id'))
        pts = [[float(stops[sid]['stop_lat']), float(stops[sid]['stop_lon'])] for _, sid in sorted(items) if sid in stops]
        if key and len(pts) > len(seq.get(key, [])):
            seq[key] = pts
    print(f"    GTFS : formes pour {sorted(shapes)} | suites d'arrêts pour {sorted(seq)}")
    return {'times': times, 'stops': stops, 'shapes': shapes, 'seq': seq, 'near': near}

def fetch_covoit():
    lock = fetch_locks['covoit']
    if not lock.acquire(blocking=False):
        return False
    try:
        print("Chargement de covoit (En Covoit' Lignes)...")
        a = get_json_retry('covoit-arrets', COVOIT_ARRETS_URL)
        if a is None:
            return False
        lig = get_json_retry('covoit-lignes', COVOIT_LIGNES_URL)

        stops = []
        for v in a.get('values', []):
            if v.get('lat') is None or v.get('lon') is None:
                continue
            stops.append({'id': v.get('gid'), 'n': v.get('nomarret'), 'a': v.get('adresse'), 'x': v.get('description'),
                          'ms': v.get('miseservice'), 'lat': round(float(v['lat']), 6), 'lon': round(float(v['lon']), 6),
                          'l': covoit_tokens(v.get('numligne')), 'h': []})

        lines = {}
        for v in (lig or {}).get('values', []):
            key = str(v.get('numligne'))
            if key not in lines:   # la ligne 7 apparaît deux fois (deux tronçons) : on garde la première description
                lines[key] = {'num': key, 'nom': v.get('nomligne'), 'o': v.get('origine'), 'd': v.get('destination'),
                              'c': v.get('codecouleur'), 'vd': v.get('voiedediee') == 'oui', 'g': []}
        for key in {t for st in stops for t in st['l']}:   # ligne présente sur des arrêts mais absente du jeu des lignes
            lines.setdefault(key, {'num': key, 'nom': f"En Covoit' Ligne {key}", 'o': None, 'd': None, 'c': None, 'vd': False, 'g': []})

        def line_for(k):
            return lines.get(str(k)) or lines.get(covoit_key(k))

        # 1) tracés : on essaie plusieurs sources jusqu'à trouver de la géométrie
        def add_geo(geo, origin):
            n = 0
            for f in (geo or {}).get('features', []):
                ln = line_for((f.get('properties') or {}).get('numligne'))
                pts = flatten_line(f.get('geometry'))
                if ln is not None and len(pts) > 1 and all(abs(p[0]) <= 90 and abs(p[1]) <= 180 for p in pts[:5]):
                    ln['g'].append(pts)
                    n += 1
            print(f"  tracés covoiturage ({origin}) : {n} tronçons")
            return n
        for origin, url in COVOIT_GEO_SOURCES:
            if add_geo(get_json_retry('covoit-tracés', url, (5, 30), tries=1), origin):
                break

        # 2) GTFS : horaires théoriques + tracés de repli
        try:
            g = read_gtfs_covoit(stops)
            for key, polys in g['shapes'].items():
                ln = line_for(key)
                if ln is not None and not ln['g']:
                    ln['g'] = polys
            for key, poly in g['seq'].items():
                ln = line_for(key)
                if ln is not None and not ln['g'] and len(poly) > 1:
                    ln['g'] = [[[round(a, 5), round(b, 5)] for a, b in poly]]
            gstops_of = {}   # arrêt En Covoit' (gid) -> arrêts GTFS voisins
            for sid, st in g['near'].items():
                gstops_of.setdefault(st['id'], []).append(sid)
            by_name = {}
            for sid, gs in g['stops'].items():
                by_name.setdefault(norm_name(gs.get('stop_name')), []).append(sid)
            for st in stops:
                cands = gstops_of.get(st['id']) or by_name.get(norm_name(st['n']), [])
                merged = {}
                for sid in cands:
                    for grp in g['times'].get(sid, []):
                        merged.setdefault((grp['l'], grp['d'], grp['h']), set()).update(grp['t'])
                st['h'] = [{'l': l, 'd': d, 'h': h, 't': sorted(ts)} for (l, d, h), ts in sorted(merged.items())]
            sans_h = [st['n'] for st in stops if not st['h']]
            print(f"  ✓ GTFS covoiturage : {len(stops) - len(sans_h)}/{len(stops)} arrêts avec horaires théoriques")
            if sans_h:
                print(f"    sans horaires : {', '.join(sans_h[:12])}{' …' if len(sans_h) > 12 else ''}")
            # Arrêts présents dans le GTFS mais absents du jeu « arrêts » (jeu parfois en retard sur les nouveaux arrêts) : on les ajoute
            known = {norm_name(st['n']) for st in stops}
            extra = {}
            for sid, gs in g['stops'].items():
                nn = norm_name(gs.get('stop_name'))
                if sid in g['near'] or nn in known or not nn:
                    continue
                try:
                    la, lo = float(gs['stop_lat']), float(gs['stop_lon'])
                except (ValueError, KeyError):
                    continue
                e = extra.setdefault(nn, {'n': gs.get('stop_name'), 'lat': la, 'lon': lo, 'g': {}})
                for grp in g['times'].get(sid, []):
                    e['g'].setdefault((grp['l'], grp['d'], grp['h']), set()).update(grp['t'])
            for nn, e in extra.items():
                hs = [{'l': l, 'd': d, 'h': h, 't': sorted(ts)} for (l, d, h), ts in sorted(e['g'].items())]
                lls = sorted({x['l'] for x in hs})
                if not lls:
                    continue
                stops.append({'id': 'gtfs-' + nn, 'n': e['n'], 'a': None, 'x': None, 'ms': None,
                              'lat': round(e['lat'], 6), 'lon': round(e['lon'], 6), 'l': lls, 'h': hs})
            if extra:
                print(f"  + {len(extra)} arrêt(s) ajouté(s) depuis le GTFS : {', '.join(e['n'] for e in extra.values())}")
        except Exception as e:
            print(f"✗ GTFS covoiturage indisponible : {e}")

        sans = [k for k, ln in lines.items() if not ln['g']]
        if sans:
            print(f"  ⚠ lignes covoiturage sans tracé : {', '.join(sans)}")
        # Contrôle : arrêts de la ligne 7 d'après le guide Sytral (mars 2026)
        attendus_l7 = {'Villeurbanne Stalingrad': 'stalingrad', 'Villeurbanne Puces du Canal': 'puces', 'Saint-Maurice-de-Beynost Porte 5': 'beynost',
                       'Montluel Gare': 'montluel', 'Niévroz Parking de la Côte': 'nievroz', 'Dagneux ZAE de la Plaine': 'dagneux',
                       'Balan Parking du Revirat': 'balan', 'Pusignan ZI du Mariage': 'mariage', 'Pusignan ZAC Satolas Green': 'satolas',
                       'Meyzieu Bd Dassault': 'dassault', 'Meyzieu Z.I.': 'meyzieuzi'}
        noms7 = [norm_name(st['n']) for st in stops if '7' in st['l']]
        manque = [lib for lib, frag in attendus_l7.items() if not any(frag in n for n in noms7)]
        print(f"  Ligne 7 : {len(noms7)} arrêts chargés" + (f" — introuvables : {', '.join(manque)}" if manque else " — tous les arrêts du guide sont présents"))
        cache['covoit'] = {'stops': stops, 'lines': list(lines.values())}
        cache['last_update']['covoit'] = time.time()
        initial_load_status['covoit'] = True
        loaded_events['covoit'].set()
        print(f"✓ covoit chargé avec succès ({len(stops)} arrêts, {len(lines)} lignes)")
        return True
    except Exception as e:
        print(f"✗ Erreur covoit: {e}")
        return False
    finally:
        lock.release()

# --- Endpoints avec cache ---
def cached_response(name, empty):
    """Sert le cache. S'il est encore vide, attend le chargement de fond (20 s max) ; sinon 503 pour que la page réessaie."""
    if cache[name] is None:
        loaded_events[name].wait(20)
    if cache[name] is None:
        if isinstance(empty, list):
            return jsonify(empty), 503
        return jsonify({**empty, "error": "données indisponibles"}), 503
    return fast_json(name, cache[name])

def fetch_buses():
    """Appelle Grand Lyon et renvoie la liste nettoyée des véhicules (lève une exception en cas d'échec)."""
    url = "https://data.grandlyon.com/siri-lite/2.0/vehicle-monitoring.json"
    r = http.get(url, timeout=TIMEOUT_BUS)
    if r.status_code != 200:
        raise RuntimeError(f"Erreur API Bus {r.status_code}")

    raw = r.json()
    delivery = raw.get('Siri', {}).get('ServiceDelivery', {}).get('VehicleMonitoringDelivery', [])
    if not delivery:
        return []

    vehicles = delivery[0].get('VehicleActivity', [])
    clean_data = []
    for v in vehicles:
        try:
            j = v.get('MonitoredVehicleJourney', {})
            pos = j.get('VehicleLocation', {})
            raw_id = j.get('VehicleRef', {}).get('value', '0000')
            parc_id = raw_id.split(':')[-2] if ':' in str(raw_id) else raw_id
            line_raw = j.get('PublishedLineName')
            if not line_raw:
                line_ref = j.get('LineRef', {}).get('value', '')
                line_raw = line_ref.split('::')[-1].split(':')[0] if '::' in line_ref else "??"

            dest_ref = j.get('DestinationRef', {}).get('value', '')
            dest_code = dest_ref.split(':')[-2] if ':' in dest_ref else '???'
            delay_raw = j.get('Delay', 'PT0S')
            delay_clean = delay_raw.replace('PT', '').replace('M', 'm ').replace('S', 's') if delay_raw else "0s"

            clean_data.append({
                "id": parc_id,
                "line": line_raw,
                "color": LINE_COLORS.get(line_raw),
                "dest_name": j.get('DestinationName', 'Inconnue'),
                "dest_code": dest_code,
                "lat": pos.get('Latitude'),
                "lon": pos.get('Longitude'),
                "bearing": j.get('Bearing', 0),
                "direction": j.get('DirectionRef', {}).get('value', 'N/C'),
                "delay": delay_clean
            })
        except Exception:
            continue
    return clean_data

@app.route('/api/buses')
def get_buses():
    """Les bus sont en temps réel : cache de 5 s seulement, et en cas de panne on renvoie les derniers bus connus"""
    age = time.time() - bus_cache['t']
    if bus_cache['data'] is not None and age < 5:
        return fast_json('buses', bus_cache['data'])
    if not bus_lock.acquire(blocking=False):
        # une autre requête interroge déjà Grand Lyon : on ne la double pas
        if bus_cache['data'] is not None:
            return fast_json('buses', bus_cache['data'])
        return jsonify([]), 503
    try:
        clean_data = fetch_buses()
        bus_cache['data'] = clean_data
        bus_cache['t'] = time.time()
        return fast_json('buses', clean_data)
    except Exception as e:
        print(f"✗ Erreur bus: {e}")
        if bus_cache['data'] is not None and time.time() - bus_cache['t'] < 120:
            return fast_json('buses', bus_cache['data'])   # données un peu vieilles plutôt que rien
        return jsonify({"error": str(e)}), 502
    finally:
        bus_lock.release()

@app.route('/api/line-trace/<ligne>')
def get_line_trace(ligne):
    """Tracés (aller / retour) d'une ligne : [{t: code_trace, s: A|R, o: origine, d: destination, c: couleur, g: [[lat,lon],…]}]"""
    if cache['lines'] is None:
        loaded_events['lines'].wait(20)
    if cache['lines'] is None:
        return jsonify({"values": [], "error": "tracés indisponibles"}), 503
    raw = cache['lines'].get(str(ligne).upper())
    if raw is None:
        return jsonify({"values": []})   # ligne sans tracé connu
    use_gzip = 'gzip' in request.headers.get('Accept-Encoding', '')
    resp = Response(gzip.compress(raw, 5) if use_gzip else raw, mimetype='application/json')
    if use_gzip:
        resp.headers['Content-Encoding'] = 'gzip'
    resp.headers['Vary'] = 'Accept-Encoding'
    return resp

@app.route('/api/covoit')
def get_covoit():
    """En Covoit' Lignes : {stops: [...], lines: [...]}"""
    return cached_response('covoit', {"stops": [], "lines": []})

@app.route('/api/parkings')
def get_parkings_cached():
    """Retourne les parkings depuis le cache"""
    return cached_response('parkings', [])

@app.route('/api/traffic')
def get_traffic_cached():
    """Retourne le trafic depuis le cache"""
    return cached_response('traffic', {"values": []})

@app.route('/api/accessibility')
def get_accessibility_cached():
    """Retourne l'accessibilité depuis le cache"""
    return cached_response('accessibility', {"values": []})

@app.route('/api/velov')
def get_velov_cached():
    """Retourne les vélos depuis le cache"""
    return cached_response('velov', {"values": []})

@app.route('/api/parkings-tr')
def get_parkings_tr_cached():
    """Retourne les places libres en temps réel des parcs relais depuis le cache"""
    return cached_response('parkings_tr', {"values": []})

@app.route('/api/stops')
def get_stops_cached():
    """Retourne les arrêts depuis le cache"""
    return cached_response('stops', {"values": []})


# --- Prochains passages aux arrêts (Grand Lyon : tclpassagearret) : temps réel (type E) et théorique (type T) ---
# Une ligne par passage : id = identifiant de l'arrêt, ligne, direction = destination, heurepassage, type E/T, idtarretdestination
PASSAGES_URL = "https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.tclpassagearret/all.json?maxfeatures=-1&start=1"
PASSAGES_EVERY = 60   # secondes : le jeu est mis à jour chaque minute, on ne le redemande pas plus souvent
# Codes de ligne du jeu de passages -> numéro affiché (bus)
PASSAGE_LINES = {'4133': '243', '5001': '231', '2004': '240', '5003': '218', '3086': '245', '3084': '165'}
passages = {'by_stop': None, 't': 0, 'error': None}
passages_lock = threading.Lock()

def refresh_passages():
    data = get_json_retry('passages', PASSAGES_URL, tries=2)
    if data is None:
        raise RuntimeError("tclpassagearret indisponible")
    now = datetime.now(PARIS_TZ).replace(tzinfo=None)
    by_stop = {}
    for v in data.get('values', []):
        try:
            when = datetime.strptime(str(v.get('heurepassage')), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        line = str(v.get('ligne') or '').strip().upper()
        by_stop.setdefault(str(v.get('id')), []).append({
            'l': PASSAGE_LINES.get(line, line), 'h': v.get('direction') or '', 'hd': v.get('idtarretdestination'),
            'e': v.get('type') == 'E',   # E = estimé en temps réel, T = théorique
            't': when.strftime('%H:%M'), 'w': when, 'dl': v.get('delaipassage') or '',
        })
    for lst in by_stop.values():
        lst.sort(key=lambda x: x['w'])
    passages['by_stop'] = by_stop
    passages['t'] = time.time()
    passages['error'] = None
    print(f"✓ prochains passages : {len(data.get('values', []))} passages sur {len(by_stop)} arrêts")
    del data
    trim_memory()

@app.route('/api/stop-passages/<stop_id>')
def get_stop_passages(stop_id):
    """Prochains passages d'un arrêt : [{l: ligne, h: destination, hd: id arrêt destination, e: temps réel ?, t: 'HH:MM', m: minutes d'attente, dl: délai affiché}]"""
    if passages['by_stop'] is None or time.time() - passages['t'] > PASSAGES_EVERY:
        if passages_lock.acquire(blocking=(passages['by_stop'] is None)):
            try:
                if passages['by_stop'] is None or time.time() - passages['t'] > PASSAGES_EVERY:
                    refresh_passages()
            except Exception as e:
                passages['error'] = str(e)
                print(f"✗ prochains passages : {e}")
            finally:
                passages_lock.release()
    if passages['by_stop'] is None:
        return jsonify({"values": [], "error": passages['error'] or "indisponible"}), 503
    now = datetime.now(PARIS_TZ).replace(tzinfo=None)
    out, per = [], Counter()
    for p in passages['by_stop'].get(str(stop_id), []):
        m = int((p['w'] - now).total_seconds() // 60)
        if m < -1:
            continue   # déjà passé
        k = (p['l'], p['h'])
        if per[k] >= 3:
            continue
        per[k] += 1
        out.append({"l": p['l'], "h": p['h'], "hd": p['hd'], "e": p['e'], "t": p['t'], "m": max(m, 0), "dl": p['dl']})
    return jsonify({"values": out[:12]})

# --- Fonction de chargement initial en arrière-plan ---
def initial_load():
    """Charge toutes les données au démarrage (sauf les bus)"""
    print("\n" + "="*50)
    print("🚀 CHARGEMENT INITIAL DES DONNÉES (hors bus)")
    print("="*50 + "\n")
    
    threads = []
    
    parking_thread = threading.Thread(target=fetch_parkings)
    parking_thread.start()
    threads.append(parking_thread)

    def heavy_loads():   # jeux lourds : l'un après l'autre pour ne pas cumuler leurs pics de mémoire
        fetch_lines()
        fetch_covoit()
        trim_memory()
    heavy_thread = threading.Thread(target=heavy_loads)
    heavy_thread.start()
    threads.append(heavy_thread)
    
    endpoints = [
        ('traffic', 'https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.tclalertetrafic_2/all.json?maxfeatures=-1&start=1'),
        ('accessibility', 'https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.tclalerteaccessibilite/all.json?maxfeatures=-1&start=1'),
        ('velov', 'https://data.grandlyon.com/fr/datapusher/ws/rdata/jcd_jcdecaux.jcdvelov/all.json?maxfeatures=-1&start=1&filename=stations-velo-v-metropole-lyon-disponibilites-temps-reel&sortby=gid'),
        ('stops', 'https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.tclarret/all.json?maxfeatures=-1&start=1'),
        ('parkings_tr', PARKINGS_TR_URL)
    ]
    
    for name, url in endpoints:
        thread = threading.Thread(target=fetch_with_cache, args=(name, url, None))
        thread.start()
        threads.append(thread)
    
    for thread in threads:
        thread.join()
    
    print("\n" + "="*50)
    print("📊 RÉSULTAT DU CHARGEMENT INITIAL")
    print("="*50)
    for name, status in initial_load_status.items():
        print(f"  {name.upper()}: {'✅ CHARGÉ' if status else '❌ ÉCHEC'}")
    print("="*50)
    trim_memory()
    print("✅ Serveur prêt !")
    print("="*50 + "\n")

# --- Fonction de rafraîchissement périodique du cache ---
def refresh_cache():
    """Rafraîchit chaque donnée à son rythme : les Vélo'v changent vite, les arrêts presque jamais"""
    every = {'parkings_tr': 60, 'velov': 60, 'traffic': 120, 'accessibility': 120, 'parkings': 3600, 'stops': 3600, 'lines': 6 * 3600, 'covoit': 6 * 3600}
    urls = {
        'traffic': "https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.tclalertetrafic_2/all.json?maxfeatures=-1&start=1",
        'accessibility': "https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.tclalerteaccessibilite/all.json?maxfeatures=-1&start=1",
        'velov': "https://data.grandlyon.com/fr/datapusher/ws/rdata/jcd_jcdecaux.jcdvelov/all.json?maxfeatures=-1&start=1",
        'stops': "https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.tclarret/all.json?maxfeatures=-1&start=1",
        'parkings_tr': PARKINGS_TR_URL,
    }
    last = {name: time.time() for name in every}
    tried = {name: 0 for name in every}
    while True:
        time.sleep(5)
        now = time.time()
        for name, delay in every.items():
            empty = cache[name] is None
            if empty:
                if now - tried[name] < 15 or fetch_locks[name].locked():   # jeu vide : on réessaie toutes les 15 s
                    continue
            elif now - last[name] < delay:
                continue
            tried[name] = last[name] = time.time()
            if name == 'parkings':
                fetch_parkings()
            elif name == 'lines':
                fetch_lines()
            elif name == 'covoit':
                fetch_covoit()
            else:
                fetch_with_cache(name, urls[name])

# --- Le site lui-même : index.html à la racine de l'adresse du service (seulement ces fichiers, jamais main.py ni le reste du dossier) ---
SITE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_FILES = {n: n for n in ('index.html', 'parc.json', 'TCL.ttf', 'manifest.json', 'sw.js', 'icon-192.png', 'icon-512.png', 'icon-maskable-512.png')}
SITE_FILES[''] = 'index.html'

@app.route('/')
@app.route('/<name>')
def site_file(name=''):
    from flask import send_from_directory, abort
    f = SITE_FILES.get(name)
    if not f or not os.path.exists(os.path.join(SITE_DIR, f)):
        abort(404)
    return send_from_directory(SITE_DIR, f)


# --- Diagnostic réseau : à ouvrir sur le Space (/api/diag) quand les données ne se chargent pas ---
@app.route('/api/diag')
def diag():
    import socket
    out = {}
    for host in ('data.grandlyon.com', 'api.github.com'):
        t0 = time.time()
        try:
            out['dns ' + host] = {'ip': socket.gethostbyname(host), 'ms': round((time.time() - t0) * 1000)}
        except Exception as e:
            out['dns ' + host] = {'erreur': str(e)}
    for name, url in (('grandlyon', 'https://data.grandlyon.com/'), ('grandlyon (avec identifiants)', 'https://data.grandlyon.com/fr/datapusher/ws/rdata/tcl_sytral.tclparcrelaistr/all.json?maxfeatures=1&start=1'),
                      ('github', 'https://api.github.com/'), ('ip sortante', 'https://api.ipify.org')):
        t0 = time.time()
        try:
            r = (http if 'identifiants' in name else requests).get(url, timeout=(8, 15))
            out[name] = {'http': r.status_code, 'ms': round((time.time() - t0) * 1000), 'corps': r.text[:60] if name == 'ip sortante' else None}
        except Exception as e:
            out[name] = {'erreur': type(e).__name__, 'ms': round((time.time() - t0) * 1000)}
    return jsonify(out)


if __name__ == '__main__':
    load_thread = threading.Thread(target=initial_load)
    load_thread.start()
    
    refresh_thread = threading.Thread(target=refresh_cache, daemon=True)
    refresh_thread.start()
    
    print("\n" + "="*50)
    print("🌐 SERVEUR EN COURS D'EXÉCUTION")
    print("="*50)
    print("Serveur démarré sur http://127.0.0.1:5000")
    print("Endpoints disponibles:")
    print("  - /api/buses         (positions des bus) [TEMPS RÉEL]")
    print("  - /api/parkings      (parkings relais) [CACHÉ]")
    print("  - /api/traffic       (alertes trafic) [CACHÉ]")
    print("  - /api/accessibility (alertes accessibilité) [CACHÉ]")
    print("  - /api/velov         (stations Velo'v) [CACHÉ]")
    print("  - /api/stops         (points d'arrêt) [CACHÉ]")
    print("  - /api/parkings-tr   (places libres des parcs relais) [CACHÉ, 60 s]")
    print("  - /api/line-trace/<ligne> (tracés aller/retour) [CACHÉ]")
    print("  - /api/covoit        (En Covoit' Lignes) [CACHÉ]")
    print("  - /api/stop-passages/<arrêt> (prochains passages, temps réel + théorique)")
    print("="*50 + "\n")
    
    # En local : 127.0.0.1:5000 (comme index.html). Hébergé (Hugging Face, Render… : variable SPACE_ID, RENDER ou PORT) : 0.0.0.0 et le port imposé
    hosted = bool(os.environ.get('SPACE_ID') or os.environ.get('RENDER') or os.environ.get('PORT'))
    app.run(host='0.0.0.0' if hosted else '127.0.0.1',
            port=int(os.environ.get('PORT') or (7860 if os.environ.get('SPACE_ID') else 5000)),
            debug=False, use_reloader=False)
