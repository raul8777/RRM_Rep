const API_BASE = 'http://localhost:8000';

// System status
async function updateSystemStatus() {
    try {
        const response = await fetch(`${API_BASE}/control/status`);
        const data = await response.json();
        
        const statusDot = document.querySelector('.status-dot');
        const statusText = document.querySelector('.status-text');
        
        statusDot.className = 'status-dot ' + data.state.toLowerCase();
        statusText.textContent = data.state;
    } catch (error) {
        console.error('Error fetching system status:', error);
    }
}

// Portfolio data
async function updatePortfolioData() {
    try {
        const response = await fetch(`${API_BASE}/config/portfolio`);
        const data = await response.json();
        
        const portfolioDiv = document.getElementById('portfolioData');
        portfolioDiv.innerHTML = `
            <div class="data-item">
                <span class="data-label">Allocation %</span>
                <span class="data-value">${data.portfolio_allocation_pct}%</span>
            </div>
            <div class="data-item">
                <span class="data-label">Min Confidence</span>
                <span class="data-value">${data.min_pair_confidence_score}</span>
            </div>
            <div class="data-item">
                <span class="data-label">Max Risk/Pair</span>
                <span class="data-value">${data.max_risk_per_pair_pct}%</span>
            </div>
            <div class="data-item">
                <span class="data-label">Diversification</span>
                <span class="data-value">${data.diversification_factor}x</span>
            </div>
            <div class="data-item">
                <span class="data-label">Position Closure</span>
                <span class="data-value">${data.enable_position_closure ? 'Enabled' : 'Disabled'}</span>
            </div>
        `;
    } catch (error) {
        console.error('Error fetching portfolio data:', error);
    }
}

// Signals data
async function updateSignalsData() {
    try {
        const response = await fetch(`${API_BASE}/signals`);
        const data = await response.json();
        
        const signalsDiv = document.getElementById('signalsData');
        if (data.signals && data.signals.length > 0) {
            signalsDiv.innerHTML = data.signals.slice(0, 5).map(signal => `
                <div class="data-item">
                    <span class="data-label">${signal.symbol}</span>
                    <span class="data-value">${signal.action}</span>
                </div>
            `).join('');
        } else {
            signalsDiv.innerHTML = '<p class="loading">No signals</p>';
        }
    } catch (error) {
        console.error('Error fetching signals data:', error);
    }
}

// Health data
async function updateHealthData() {
    try {
        const response = await fetch(`${API_BASE}/health`);
        const data = await response.json();
        
        const healthDiv = document.getElementById('healthData');
        healthDiv.innerHTML = `
            <div class="data-item">
                <span class="data-label">System State</span>
                <span class="data-value">${data.system_state}</span>
            </div>
            <div class="data-item">
                <span class="data-label">Trade Cycle</span>
                <span class="data-value">${data.trade_cycle_running ? 'Running' : 'Stopped'}</span>
            </div>
            <div class="data-item">
                <span class="data-label">Analysis Cycle</span>
                <span class="data-value">${data.analysis_cycle_running ? 'Running' : 'Stopped'}</span>
            </div>
            <div class="data-item">
                <span class="data-label">Portfolio Manager</span>
                <span class="data-value">${data.portfolio_manager_active ? 'Active' : 'Inactive'}</span>
            </div>
            <div class="data-item">
                <span class="data-label">Position Manager</span>
                <span class="data-value">${data.position_manager_active ? 'Active' : 'Inactive'}</span>
            </div>
        `;
    } catch (error) {
        console.error('Error fetching health data:', error);
    }
}

// Control buttons
document.getElementById('pauseBtn').addEventListener('click', async () => {
    try {
        await fetch(`${API_BASE}/control/pause`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: 'Manual pause from dashboard' })
        });
        updateSystemStatus();
    } catch (error) {
        console.error('Error pausing system:', error);
    }
});

document.getElementById('stopBtn').addEventListener('click', async () => {
    try {
        await fetch(`${API_BASE}/control/stop`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: 'Manual stop from dashboard' })
        });
        updateSystemStatus();
    } catch (error) {
        console.error('Error stopping system:', error);
    }
});

document.getElementById('startBtn').addEventListener('click', async () => {
    try {
        await fetch(`${API_BASE}/control/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: 'Manual start from dashboard' })
        });
        updateSystemStatus();
    } catch (error) {
        console.error('Error starting system:', error);
    }
});

// Initialize and update periodically
function initDashboard() {
    updateSystemStatus();
    updatePortfolioData();
    updateSignalsData();
    updateHealthData();
    
    // Update every 5 seconds
    setInterval(() => {
        updateSystemStatus();
        updatePortfolioData();
        updateSignalsData();
        updateHealthData();
    }, 5000);
}

// Start dashboard when page loads
document.addEventListener('DOMContentLoaded', initDashboard);
