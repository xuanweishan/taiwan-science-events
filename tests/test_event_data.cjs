const {test} = require('node:test');
const assert = require('node:assert/strict');
const findEventData = require('../.github/scripts/find-event-data.cjs');

function artifact(id, overrides = {}) {
  return {id, name: 'event-data', expired: false,
    created_at: `2026-10-${String(id).padStart(2, '0')}T00:00:00Z`,
    workflow_run: {id: id * 10, head_branch: 'main', repository_id: 1, head_repository_id: 1},
    ...overrides};
}

function fixture(artifacts, runs = {}) {
  const outputs = {}, warnings = [], inspected = [];
  const github = {rest: {actions: {
    getWorkflow: async () => ({data: {id: 42}}),
    listArtifactsForRepo: () => {},
    getWorkflowRun: async ({run_id}) => {
      inspected.push(run_id);
      return {data: {id: run_id, workflow_id: 42, status: 'completed',
        conclusion: 'success', head_branch: 'main', ...runs[run_id]}};
    },
  }}, paginate: async (_method, options) => {
    assert.equal(options.name, 'event-data');
    assert.equal(options.per_page, 100);
    return artifacts;
  }};
  return {github, context: {repo: {owner: 'owner', repo: 'events'}, runId: 999},
    core: {setOutput: (key, value) => {outputs[key] = value;},
      warning: message => warnings.push(message), info: () => {}},
    outputs, warnings, inspected};
}

test('restores the newest snapshot even when API results are unordered', async () => {
  const f = fixture([artifact(1), artifact(3), artifact(2)]);
  await findEventData(f);
  assert.deepEqual(f.outputs, {'artifact-id': '3', 'run-id': '30', found: 'true'});
  assert.deepEqual(f.inspected, [30]);
  assert.equal(f.warnings.length, 0);
});

test('skips failed, incomplete and unrelated workflows', async () => {
  const f = fixture([artifact(1), artifact(2), artifact(3), artifact(4)], {
    40: {conclusion: 'failure'}, 30: {status: 'in_progress', conclusion: null},
    20: {workflow_id: 77},
  });
  await findEventData(f);
  assert.equal(f.outputs['artifact-id'], '1');
  assert.deepEqual(f.inspected, [40, 30, 20, 10]);
});

test('rejects expired, other-branch, fork, other-name and current-run artifacts', async () => {
  const f = fixture([
    artifact(1), artifact(2, {expired: true}), artifact(3, {name: 'other'}),
    artifact(4, {workflow_run: {id: 40, head_branch: 'feature'}}),
    artifact(5, {workflow_run: {id: 50, head_branch: 'main', repository_id: 1, head_repository_id: 2}}),
    artifact(6, {workflow_run: {id: 999, head_branch: 'main', repository_id: 1, head_repository_id: 1}}),
  ]);
  await findEventData(f);
  assert.deepEqual(f.inspected, [10]);
  assert.equal(f.outputs.found, 'true');
});

test('first run and expired snapshots explicitly fall back to repository seed', async () => {
  for (const artifacts of [[], [artifact(1, {expired: true})]]) {
    const f = fixture(artifacts);
    await findEventData(f);
    assert.deepEqual(f.outputs, {found: 'false'});
    assert.equal(f.warnings.length, 1);
  }
});

test('does not fall back silently on API/authentication errors', async () => {
  const f = fixture([]);
  f.github.paginate = async () => {throw Error('403 denied');};
  await assert.rejects(findEventData(f), /403 denied/);
  assert.deepEqual(f.outputs, {});
  assert.equal(f.warnings.length, 0);
});

test('does not restore an artifact when run metadata disagrees on branch', async () => {
  const f = fixture([artifact(1)], {10: {head_branch: 'feature'}});
  await findEventData(f);
  assert.equal(f.outputs.found, 'false');
});
