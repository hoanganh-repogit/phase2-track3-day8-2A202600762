// Web application state
let scenarios = [];
let executionResults = {}; // scenario_id -> metric payload
let activeEventSource = null;
let currentRunningScenarioId = null;

// DOM Elements
const scenariosList = document.getElementById('scenarios-list');
const runAllBtn = document.getElementById('run-all-btn');
const recordBtn = document.getElementById('record-btn');
const activeScenarioId = document.getElementById('active-scenario-id');
const activeScenarioQuery = document.getElementById('active-scenario-query');
const consoleLog = document.getElementById('console-log');
const stateJsonViewer = document.getElementById('state-json-viewer');

// Recording State
let mediaRecorder = null;
let recordedChunks = [];

// Metrics elements
const metricSuccessRate = document.getElementById('metric-success-rate');
const metricProgress = document.getElementById('metric-progress');
const metricAvgNodes = document.getElementById('metric-avg-nodes');
const metricRetriesInterrupts = document.getElementById('metric-retries-interrupts');

// HITL Modal elements
const hitlModal = document.getElementById('hitl-modal');
const hitlActionText = document.getElementById('hitl-action-text');
const hitlComment = document.getElementById('hitl-comment');
const hitlApproveBtn = document.getElementById('hitl-approve-btn');
const hitlRejectBtn = document.getElementById('hitl-reject-btn');
let currentInterruptThreadId = null;

// Initialize app
document.addEventListener('DOMContentLoaded', async () => {
    await fetchScenarios();
    setupEventListeners();
});

// Fetch scenarios from API
async function fetchScenarios() {
    try {
        const res = await fetch('/api/scenarios');
        scenarios = await res.json();
        renderScenariosList();
        updateMetrics();
    } catch (err) {
        console.error('Error fetching scenarios:', err);
        logMessage('System', 'Error fetching scenarios from API backend.', 'error');
    }
}

// Render scenario list in sidebar
function renderScenariosList() {
    scenariosList.innerHTML = '';
    scenarios.forEach(sc => {
        const item = document.createElement('div');
        item.className = `scenario-item`;
        item.id = `sc-${sc.id}`;
        
        let statusClass = 'status-pending';
        let statusLabel = 'Pending';
        
        if (executionResults[sc.id]) {
            const isSuccess = executionResults[sc.id].success;
            statusClass = isSuccess ? 'status-success' : 'status-failed';
            statusLabel = isSuccess ? 'Success' : 'Failed';
        } else if (currentRunningScenarioId === sc.id) {
            statusClass = 'status-running';
            statusLabel = 'Running';
        }

        item.innerHTML = `
            <div class="scenario-meta">
                <span class="scenario-id-tag">${sc.id}</span>
                <span class="scenario-status-badge ${statusClass}">${statusLabel}</span>
            </div>
            <p class="scenario-query">${sc.query}</p>
        `;
        
        item.addEventListener('click', () => {
            if (!currentRunningScenarioId) {
                runScenario(sc.id);
            } else {
                logMessage('System', 'Please wait for the current scenario run to complete.', 'info');
            }
        });
        
        scenariosList.appendChild(item);
    });
}

// Setup listeners for buttons and modals
function setupEventListeners() {
    runAllBtn.addEventListener('click', () => {
        if (!currentRunningScenarioId) {
            runAllScenarios();
        }
    });

    recordBtn.addEventListener('click', () => {
        toggleRecording();
    });

    hitlApproveBtn.addEventListener('click', () => submitHITLDecision(true));
    hitlRejectBtn.addEventListener('click', () => submitHITLDecision(false));
}

// Log message to virtual console
function logMessage(node, message, type = 'info') {
    const entry = document.createElement('div');
    entry.className = `log-entry log-${type}`;
    entry.innerHTML = `<span class="log-node">[${node}]</span> ${message}`;
    consoleLog.appendChild(entry);
    consoleLog.scrollTop = consoleLog.scrollHeight;
}

// Reset flow chart styles
function resetGraphVisuals() {
    document.querySelectorAll('.node').forEach(node => {
        node.className = 'node';
    });
}

// Update flowchart nodes classes
function updateNodeHighlight(activeNode) {
    // List of nodes in flow
    const nodeIds = [
        'intake', 'classify', 'clarify', 'tool', 'evaluate', 'approval', 'retry', 'answer', 'dead_letter', 'finalize'
    ];
    
    nodeIds.forEach(id => {
        const nodeEl = document.getElementById(`node-${id}`);
        if (!nodeEl) return;
        
        if (id === activeNode) {
            nodeEl.className = 'node active-node';
        } else if (nodeEl.classList.contains('active-node')) {
            nodeEl.className = 'node completed-node';
        }
    });
}

// Stream a single scenario
function runScenario(scenarioId) {
    return new Promise((resolve) => {
        const sc = scenarios.find(s => s.id === scenarioId);
        if (!sc) return resolve();

        // Update UI states
        currentRunningScenarioId = scenarioId;
        renderScenariosList();
        
        // Highlight active item
        document.querySelectorAll('.scenario-item').forEach(el => el.classList.remove('active'));
        document.getElementById(`sc-${scenarioId}`).classList.add('active');

        // Populate detail panels
        activeScenarioId.innerText = sc.id;
        activeScenarioQuery.innerText = sc.query;
        consoleLog.innerHTML = '';
        stateJsonViewer.innerText = '{}';
        resetGraphVisuals();

        logMessage('System', `Initiating scenario ${sc.id}...`, 'info');

        // Connect SSE stream
        activeEventSource = new EventSource(`/api/run-scenario/stream?scenario_id=${scenarioId}`);

        activeEventSource.addEventListener('init', (e) => {
            const data = JSON.parse(e.data);
            logMessage('System', `Thread ID: ${data.thread_id}`, 'info');
        });

        activeEventSource.addEventListener('node_update', (e) => {
            const data = JSON.parse(e.data);
            const node = data.node;
            
            logMessage(node, `Node execution step completed.`);
            updateNodeHighlight(node);
            
            // Render full state JSON
            stateJsonViewer.innerText = JSON.stringify(data.full_state, null, 2);
        });

        activeEventSource.addEventListener('interrupt', (e) => {
            const data = JSON.parse(e.data);
            currentInterruptThreadId = data.thread_id;
            
            const payload = data.payload || {};
            hitlActionText.innerText = payload.proposed_action || 'Prepare risky action for approval';
            
            // Show modal
            hitlModal.classList.add('open');
            logMessage('approval', 'Graph execution paused. Awaiting human-in-the-loop approval.', 'warning');
            updateNodeHighlight('approval');
        });

        activeEventSource.addEventListener('completed', (e) => {
            const metric = JSON.parse(e.data);
            executionResults[scenarioId] = metric;
            logMessage('finalize', `Scenario completed successfully. Expected route: ${metric.expected_route}, Actual route: ${metric.actual_route}`, 'completed');
            updateNodeHighlight('finalize');
            
            closeConnection();
            resolve();
        });

        activeEventSource.addEventListener('error', (e) => {
            const data = JSON.parse(e.data);
            logMessage('error', `Execution failed: ${data.error}`, 'error');
            executionResults[scenarioId] = { success: false };
            
            closeConnection();
            resolve();
        });
    });
}

// Close connection and refresh metrics
function closeConnection() {
    if (activeEventSource) {
        activeEventSource.close();
        activeEventSource = null;
    }
    currentRunningScenarioId = null;
    renderScenariosList();
    updateMetrics();
}

// Send human decision (Approve/Reject)
async function submitHITLDecision(approved) {
    if (!currentInterruptThreadId) return;
    
    try {
        const comment = hitlComment.value;
        const res = await fetch('/api/approve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                thread_id: currentInterruptThreadId,
                approved: approved,
                comment: comment
            })
        });
        
        if (res.ok) {
            hitlModal.classList.remove('open');
            hitlComment.value = '';
            currentInterruptThreadId = null;
            logMessage('approval', `Decision submitted: ${approved ? 'APPROVED' : 'REJECTED'}. Resuming flow.`, 'completed');
        } else {
            console.error('Error posting approval');
        }
    } catch (err) {
        console.error('API Error submitting approval decision:', err);
    }
}

// Run all scenarios sequentially
async function runAllScenarios() {
    logMessage('System', 'Starting batch run of all scenarios...', 'info');
    executionResults = {}; // Reset previous results
    
    for (let i = 0; i < scenarios.length; i++) {
        const sc = scenarios[i];
        await runScenario(sc.id);
        // Pause briefly between scenarios
        await new Promise(r => setTimeout(r, 1000));
    }
    
    logMessage('System', 'All scenarios execution completed.', 'completed');
}

// Recalculate metrics dynamically based on results
function updateMetrics() {
    const executed = Object.values(executionResults);
    const totalCount = scenarios.length;
    const runCount = executed.length;
    
    metricProgress.innerText = `${runCount} / ${totalCount}`;

    if (runCount === 0) {
        metricSuccessRate.innerText = '0%';
        metricAvgNodes.innerText = '0.0';
        metricRetriesInterrupts.innerText = '0 / 0';
        return;
    }

    const successCount = executed.filter(r => r.success).length;
    const rate = Math.round((successCount / runCount) * 100);
    metricSuccessRate.innerText = `${rate}%`;

    const totalNodes = executed.reduce((sum, r) => sum + (r.nodes_visited || 0), 0);
    const avgNodes = (totalNodes / runCount).toFixed(1);
    metricAvgNodes.innerText = avgNodes;

    const totalRetries = executed.reduce((sum, r) => sum + (r.retry_count || 0), 0);
    const totalInterrupts = executed.reduce((sum, r) => sum + (r.interrupt_count || 0), 0);
    metricRetriesInterrupts.innerText = `${totalRetries} / ${totalInterrupts}`;
}

// Native screen recorder using MediaRecorder API
async function toggleRecording() {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
        mediaRecorder.stop();
        return;
    }

    recordedChunks = [];
    try {
        const stream = await navigator.mediaDevices.getDisplayMedia({
            video: {
                displaySurface: "browser",
            },
            audio: false
        });

        mediaRecorder = new MediaRecorder(stream, { mimeType: 'video/webm' });

        mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0) {
                recordedChunks.push(event.data);
            }
        };

        mediaRecorder.onstop = () => {
            stream.getTracks().forEach(track => track.stop());

            const blob = new Blob(recordedChunks, { type: 'video/webm' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = `dashboard_run_${new Date().toISOString().slice(0,19).replace(/T|:/g, '_')}.webm`;
            document.body.appendChild(a);
            a.click();
            setTimeout(() => {
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            }, 100);

            recordBtn.innerHTML = '<i class="fa-solid fa-video"></i> Record Run Video';
            recordBtn.style.background = 'rgba(255,255,255,0.08)';
            recordBtn.style.borderColor = 'rgba(255,255,255,0.15)';
            logMessage('System', 'Recording stopped and file downloaded.', 'completed');
        };

        mediaRecorder.start();

        recordBtn.innerHTML = '<i class="fa-solid fa-square"></i> Stop Recording';
        recordBtn.style.background = 'rgba(244, 63, 94, 0.2)';
        recordBtn.style.borderColor = 'var(--danger)';
        logMessage('System', 'Screen recording started. Please share this tab/window to capture.', 'info');

    } catch (err) {
        console.error('Error starting screen recording:', err);
        logMessage('System', 'Screen recording cancelled or failed.', 'error');
    }
}
