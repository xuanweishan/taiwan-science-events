// Called by actions/github-script; no extra npm dependencies are required.
module.exports = async ({github, context, core}) => {
  const repo = context.repo;
  const {data: workflow} = await github.rest.actions.getWorkflow({
    ...repo, workflow_id: 'pages.yml',
  });
  const artifacts = await github.paginate(github.rest.actions.listArtifactsForRepo, {
    ...repo, name: 'event-data', per_page: 100,
  });
  const candidates = artifacts.filter(artifact =>
    artifact.name === 'event-data' && !artifact.expired &&
    artifact.workflow_run?.head_branch === 'main' &&
    artifact.workflow_run.repository_id === artifact.workflow_run.head_repository_id &&
    artifact.workflow_run.id !== context.runId
  ).sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id);

  for (const artifact of candidates) {
    const {data: run} = await github.rest.actions.getWorkflowRun({
      ...repo, run_id: artifact.workflow_run.id,
    });
    if (run.workflow_id !== workflow.id || run.status !== 'completed' ||
        run.conclusion !== 'success' || run.head_branch !== 'main') continue;
    core.setOutput('artifact-id', String(artifact.id));
    core.setOutput('run-id', String(run.id));
    core.setOutput('found', 'true');
    core.info(`Restoring event-data from successful workflow run ${run.id}.`);
    return;
  }

  // API/auth/download failures must fail the job, rather than deploy seed data.
  // This fallback is only for first use, or when all snapshots have expired.
  core.setOutput('found', 'false');
  core.warning('No retained event-data from a successful main-branch run. Using the repository seed JSON; historical coverage may be reduced.');
};
