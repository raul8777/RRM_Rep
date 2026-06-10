const API_BASE = 'http://localhost:8000';

// Load current configuration
async function loadCurrentConfig() {
    try {
        const response = await fetch(`${API_BASE}/config/portfolio`);
        const data = await response.json();
        
        // Populate form fields
        document.getElementById('portfolioAllocationPct').value = data.portfolio_allocation_pct;
        document.getElementById('minPairConfidenceScore').value = data.min_pair_confidence_score;
        document.getElementById('maxRiskPerPairPct').value = data.max_risk_per_pair_pct;
        document.getElementById('diversificationFactor').value = data.diversification_factor;
        document.getElementById('enablePositionClosure').checked = data.enable_position_closure;
        
        // Display current config
        const configDiv = document.getElementById('currentConfig');
        configDiv.innerHTML = `
            <div class="data-item">
                <span class="data-label">Portfolio Allocation %</span>
                <span class="data-value">${data.portfolio_allocation_pct}%</span>
            </div>
            <div class="data-item">
                <span class="data-label">Min Pair Confidence Score</span>
                <span class="data-value">${data.min_pair_confidence_score}</span>
            </div>
            <div class="data-item">
                <span class="data-label">Max Risk Per Pair %</span>
                <span class="data-value">${data.max_risk_per_pair_pct}%</span>
            </div>
            <div class="data-item">
                <span class="data-label">Diversification Factor</span>
                <span class="data-value">${data.diversification_factor}x</span>
            </div>
            <div class="data-item">
                <span class="data-label">Enable Position Closure</span>
                <span class="data-value">${data.enable_position_closure ? 'Enabled' : 'Disabled'}</span>
            </div>
        `;
    } catch (error) {
        console.error('Error loading configuration:', error);
        document.getElementById('currentConfig').innerHTML = '<p class="loading">Error loading configuration</p>';
    }
}

// Save configuration
document.getElementById('portfolioConfigForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const formData = {
        portfolio_allocation_pct: parseFloat(document.getElementById('portfolioAllocationPct').value),
        min_pair_confidence_score: parseFloat(document.getElementById('minPairConfidenceScore').value),
        max_risk_per_pair_pct: parseFloat(document.getElementById('maxRiskPerPairPct').value),
        diversification_factor: parseFloat(document.getElementById('diversificationFactor').value),
        enable_position_closure: document.getElementById('enablePositionClosure').checked
    };
    
    try {
        const response = await fetch(`${API_BASE}/config/portfolio`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(formData)
        });
        
        if (response.ok) {
            alert('Configuration saved successfully!');
            loadCurrentConfig();
        } else {
            alert('Error saving configuration');
        }
    } catch (error) {
        console.error('Error saving configuration:', error);
        alert('Error saving configuration');
    }
});

// Reset to defaults
document.getElementById('resetBtn').addEventListener('click', () => {
    document.getElementById('portfolioAllocationPct').value = 100.0;
    document.getElementById('minPairConfidenceScore').value = 50.0;
    document.getElementById('maxRiskPerPairPct').value = 10.0;
    document.getElementById('diversificationFactor').value = 1.0;
    document.getElementById('enablePositionClosure').checked = true;
});

// Load configuration on page load
document.addEventListener('DOMContentLoaded', loadCurrentConfig);
