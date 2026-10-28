import { readFile } from 'node:fs/promises';

async function upload([reportPath, project, version], apiKey) {
  if (!reportPath || !project || !version) {
    console.error('usage: node upload-stryker-report.mjs <mutation-report.json> <project> <version>');
    return 2;
  }
  if (!apiKey?.trim()) {
    console.error('STRYKER_DASHBOARD_API_KEY is not in the environment');
    return 2;
  }
  const url = `https://dashboard.stryker-mutator.io/api/reports/${project}/${version}`;
  const response = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', 'X-Api-Key': apiKey },
    body: await readFile(reportPath),
  });
  const answer = await response.text();
  if (!response.ok) {
    console.error(`the dashboard answered ${response.status} for ${url}: ${answer}`);
    return 1;
  }
  console.log(`uploaded ${reportPath} to ${url}: ${response.status} ${answer}`);
  return 0;
}

process.exitCode = await upload(process.argv.slice(2), process.env.STRYKER_DASHBOARD_API_KEY);
