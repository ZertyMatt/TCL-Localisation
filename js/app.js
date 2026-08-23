// ============================================
// INITIALISATION DE LA CARTE
// ============================================
window.map = L.map('map', {
    zoomControl: false,
    attributionControl: false,
    renderer: L.canvas()
}).setView([45.757, 4.832], 13);

// Ajouter le fond de carte
L.tileLayer('https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png', {
    subdomains: 'abc',
    maxZoom: 20,
    attribution: '© OpenStreetMap France | © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
}).addTo(window.map);

// Créer les panes personnalisés pour le z-index
window.map.createPane('tracePane');
window.map.getPane('tracePane').style.zIndex = 390;
window.map.createPane('parkingPane');
window.map.getPane('parkingPane').style.zIndex = 410;
window.map.createPane('velovPane');
window.map.getPane('velovPane').style.zIndex = 420;
window.map.createPane('stopPane');
window.map.getPane('stopPane').style.zIndex = 430;
window.map.createPane('vehiclePane');
window.map.getPane('vehiclePane').style.zIndex = 440;

// Créer les couches
window.parkingLayer = L.layerGroup().addTo(window.map);
window.parcsRelaisLayer = L.layerGroup().addTo(window.map);
window.lineTraceLayer = L.layerGroup().addTo(window.map);
window.velovLayer = L.layerGroup().addTo(window.map);
window.busLayer = L.layerGroup().addTo(window.map);
window.agenceLayer = L.layerGroup().addTo(window.map);
window.stopMapLayer = L.layerGroup().addTo(window.map);

// ============================================
// FONCTIONS DE GESTION DES COUCHES
// ============================================

window.toggleLayer = function(layerName) {
    window.layerVisibility[layerName] = !window.layerVisibility[layerName];
    const btn = document.querySelector(`[data-layer="${layerName}"]`);
    if (btn) btn.classList.toggle('active', window.layerVisibility[layerName]);

    if (layerName === 'bus') {
        if (window.layerVisibility.bus) {
            window.map.addLayer(window.busLayer);
            window.applyBusLineFilter();
        } else {
            window.map.removeLayer(window.busLayer);
        }
    }
    if (layerName === 'stops') {
        if (window.layerVisibility.stops) {
            window.map.addLayer(window.stopMapLayer);
            window.renderVisibleStops();
        } else {
            window.map.removeLayer(window.stopMapLayer);
            window.stopMapLayer.clearLayers();
        }
    }
    if (layerName === 'velov') {
        if (window.layerVisibility.velov) {
            window.map.addLayer(window.velovLayer);
            window.updateVisibleVelov();
        } else {
            window.map.removeLayer(window.velovLayer);
            window.velovLayer.clearLayers();
            window.velovMarkerMap.clear();
        }
    }
    if (layerName === 'parking') {
        if (window.layerVisibility.parking) {
            window.map.addLayer(window.parkingLayer);
            window.map.addLayer(window.parcsRelaisLayer);
            window.updateVisibleParkings();
            window.updateVisibleParkAndRideLots();
        } else {
            window.map.removeLayer(window.parkingLayer);
            window.map.removeLayer(window.parcsRelaisLayer);
            window.parkingLayer.clearLayers();
            window.parcsRelaisLayer.clearLayers();
            window.parkingMarkerMap.clear();
            window.parcsRelaisMarkerMap.clear();
        }
    }
};

window.toggleAllLayers = function() {
    const allVisible = Object.values(window.layerVisibility).every(v => v);
    Object.keys(window.layerVisibility).forEach(layer => {
        window.layerVisibility[layer] = !allVisible;
        const btn = document.querySelector(`[data-layer="${layer}"]`);
        if (btn) btn.classList.toggle('active', window.layerVisibility[layer]);
    });
    window.toggleLayer('bus');
    window.toggleLayer('stops');
    window.toggleLayer('velov');
    window.toggleLayer('parking');
};

// ============================================
// FONCTIONS DE FILTRAGE
// ============================================

window.filterStops = function() {
    const term = document.getElementById('stop-search').value.toLowerCase().trim();
    const container = document.getElementById('stops-list');
    if (!container) return;

    const filtered = window.allStops.filter(s =>
        s.nom && s.nom.toLowerCase().includes(term)
    );

    const stats = document.getElementById('search-stats');
    if (stats) stats.textContent = `${filtered.length}/${window.allStops.length}`;

    if (filtered.length === 0) {
        container.innerHTML = '<div class="info-empty">Aucun arrêt trouvé</div>';
        return;
    }

    container.innerHTML = filtered.map(s => `
        <div class="info-item" onclick="window.toggleInfoItem(this)">
            <div class="info-item-header">
                <span class="info-item-icon">📍</span>
                <span class="info-item-title">${s.nom || 'Arrêt sans nom'}</span>
            </div>
            <div class="info-item-body">
                <p>${window.renderLineSvgs(window.extractLines(s.desserte))}</p>
                <p class="mono">${s.id || ''}</p>
            </div>
        </div>
    `).join('');
};

window.filterLines = function() {
    const filters = {
        metro: document.getElementById('filter-metro').checked,
        tram: document.getElementById('filter-tram').checked,
        tb: document.getElementById('filter-tb').checked,
        funicular: document.getElementById('filter-funicular').checked,
        navgone: document.getElementById('filter-navgone').checked,
        chrono: document.getElementById('filter-chrono').checked,
        bus: document.getElementById('filter-bus').checked,
        navette: document.getElementById('filter-navette').checked,
        pl: document.getElementById('filter-pl').checked,
        jd: document.getElementById('filter-jd').checked
    };

    const container = document.getElementById('lines-list');
    if (!container) return;

    const filteredLines = window.allLines.filter(line => {
        const type = window.getLineType(line);
        return filters[type];
    });

    if (filteredLines.length === 0) {
        container.innerHTML = '<div class="info-empty">Aucune ligne correspondante</div>';
        return;
    }

    container.innerHTML = filteredLines.map(line => `
        <div class="info-item" onclick="window.filterLineFromPopup('${line}')">
            <div class="info-item-header">
                <span class="info-item-icon">${window.lineImgHtml(line, '20px')}</span>
                <span class="info-item-title">${line}</span>
            </div>
            <div class="info-item-body">
                <p style="color:${window.getLineColor(line)}">${window.getLineType(line)}</p>
            </div>
        </div>
    `).join('');
};

// ============================================
// FONCTIONS D'AFFICHAGE DES ARRÊTS
// ============================================

window.renderStopsOnMap = function() {
    if (!window.layerVisibility.stops) {
        window.stopMapLayer.clearLayers();
        return;
    }

    const zoom = window.map.getZoom();
    if (zoom < 13) {
        window.stopMapLayer.clearLayers();
        return;
    }

    const bounds = window.map.getBounds();
    const visible = window.allStops.filter(s => {
        const lat = parseFloat(s.lat), lon = parseFloat(s.lon);
        return !isNaN(lat) && !isNaN(lon) && bounds.contains([lat, lon]);
    }).slice(0, window.CONFIG.MAX_STOPS_ON_MAP);

    window.stopMapLayer.clearLayers();

    visible.forEach(s => {
        const lines = window.extractLines(s.desserte);
        const html = `
            <div style="position:relative;width:24px;height:24px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));">
                <div style="width:24px;height:24px;background:rgba(13,15,24,0.9);border-radius:50%;display:flex;align-items:center;justify-content:center;">
                    <span style="font-size:12px;">📍</span>
                </div>
            </div>`;

        const marker = L.marker([parseFloat(s.lat), parseFloat(s.lon)], {
            pane: 'stopPane',
            icon: L.divIcon({
                html: html,
                className: '',
                iconSize: [24, 24],
                iconAnchor: [12, 12]
            })
        }).bindPopup(`
            <div style="width:240px;background:var(--glass-bg-heavy);border-radius:16px;padding:12px;font-family:inherit;border:1px solid var(--glass-border-highlight);">
                <div style="font-size:14px;font-weight:600;color:var(--text-primary);margin-bottom:8px;">${s.nom || 'Arrêt sans nom'}</div>
                <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px;">${window.renderLineSvgs(lines)}</div>
                <div style="font-size:11px;color:var(--text-secondary);">ID: ${s.id}</div>
            </div>
        `).addTo(window.stopMapLayer);
    });
};

window.renderStopList = function(term = '') {
    const container = document.getElementById('stops-list');
    if (!container) return;

    const filtered = term ?
        window.allStops.filter(s => s.nom && s.nom.toLowerCase().includes(term.toLowerCase())) :
        window.allStops;

    const stats = document.getElementById('search-stats');
    if (stats) stats.textContent = `${filtered.length}/${window.allStops.length}`;

    if (filtered.length === 0) {
        container.innerHTML = '<div class="info-empty">Aucun arrêt trouvé</div>';
        return;
    }

    container.innerHTML = filtered.slice(0, 50).map(s => `
        <div class="info-item" onclick="window.toggleInfoItem(this)">
            <div class="info-item-header">
                <span class="info-item-icon">📍</span>
                <span class="info-item-title">${s.nom || 'Arrêt sans nom'}</span>
            </div>
            <div class="info-item-body">
                <p>${window.renderLineSvgs(window.extractLines(s.desserte))}</p>
                <p class="mono">${s.id || ''}</p>
            </div>
        </div>
    `).join('');
};

// ============================================
// INITIALISATION
// ============================================

window.init = async function() {
    window.showSpinner();
    try {
        await window.loadInitialData();
        setTimeout(window.checkSystemStatus, 3000);
    } catch (e) {
        window.showNotification("Erreur d'initialisation", "error");
        console.error("Erreur init:", e);
    } finally {
        window.hideSpinner();
    }
};

// Démarrer l'application quand le DOM est prêt
document.addEventListener('DOMContentLoaded', function() {
    // Initialiser la carte
    setTimeout(window.init, 100);

    // Rafraîchir les données périodiquement
    setInterval(window.updateBus, 30000);
    setInterval(window.updateVelov, 120000);
    setInterval(window.updateTraffic, 120000);
    setInterval(window.updateAccessibility, 120000);
    setInterval(window.updateStopsData, 600000);
    setInterval(window.checkSystemStatus, 30000);

    // Corriger la taille de la carte après un délai
    setTimeout(() => window.map.invalidateSize(), 300);

    // Gérer le mouvement de la carte
    let moveTimer = null;
    window.map.on('moveend zoomend', () => {
        clearTimeout(moveTimer);
        moveTimer = setTimeout(() => {
            window.updateVisibleVelov();
            window.updateVisibleParkings();
            window.updateVisibleParkAndRideLots();
            window.renderStopsOnMap();
            window.updateBusIcons();
        }, 200);
    }, { passive: true });

    // Gérer le redimensionnement
    window.addEventListener('resize', () => {
        setTimeout(() => window.map.invalidateSize(), 250);
        const panel = document.getElementById('sidebar');
        if (panel && panel.classList.contains('visible')) {
            panel.style.bottom = '0';
        }
    });

    // Gérer le zoom
    window.map.on('zoomend', () => {
        window.updateIconSizes();
    });
});
