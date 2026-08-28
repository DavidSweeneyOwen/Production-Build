/**
 * CheckFire Stock Board - refresh trigger (Azure Function, Node v4)
 *
 * The board POSTs here; this starts the "Refresh stock data" workflow on
 * GitHub. It never touches NetSuite - all it does is press the button.
 *
 * The GitHub token lives in App Settings and never reaches the browser.
 *
 * App settings to configure:
 *   GH_TOKEN   fine-grained PAT, this repo only, Actions = Read and write
 *   GH_REPO    DavidSweeneyOwen/Production-Build
 *   GH_BRANCH  main            (optional, defaults to main)
 *
 * CORS is configured on the Function App itself (API -> CORS), not here.
 */

const { app } = require("@azure/functions");

const WORKFLOW_FILE = "refresh.yml";
const MIN_GAP_MS = 3 * 60 * 1000;   // refuse a run within 3 minutes of the last

function json(body, status) {
  return { status, jsonBody: body, headers: { "Cache-Control": "no-store" } };
}

app.http("refresh", {
  methods: ["POST"],
  authLevel: "function",
  handler: async (request, context) => {
    const token = process.env.GH_TOKEN;
    const repo = process.env.GH_REPO;
    const branch = process.env.GH_BRANCH || "main";

    if (!token || !repo) {
      context.error("GH_TOKEN or GH_REPO app setting is missing");
      return json({ status: "error", message: "Function is not configured" }, 500);
    }

    const gh = (path, init = {}) =>
      fetch(`https://api.github.com/repos/${repo}${path}`, {
        ...init,
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "checkfire-stock-board",
          ...(init.headers || {}),
        },
      });

    // Throttle against the most recent run.
    try {
      const res = await gh(`/actions/workflows/${WORKFLOW_FILE}/runs?per_page=1`);
      if (res.ok) {
        const last = (await res.json()).workflow_runs?.[0];
        if (last) {
          const age = Date.now() - new Date(last.created_at).getTime();
          if (last.status !== "completed") {
            return json({ status: "running", message: "A refresh is already running." }, 202);
          }
          if (age < MIN_GAP_MS) {
            return json({
              status: "throttled",
              message: `Last refresh ran ${Math.round(age / 1000)}s ago.`,
              retryInSeconds: Math.ceil((MIN_GAP_MS - age) / 1000),
            }, 429);
          }
        }
      }
    } catch (err) {
      context.warn(`Throttle check failed, dispatching anyway: ${err.message}`);
    }

    const res = await gh(`/actions/workflows/${WORKFLOW_FILE}/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref: branch }),
      headers: { "Content-Type": "application/json" },
    });

    if (res.status === 204) return json({ status: "queued" }, 202);

    const detail = (await res.text()).slice(0, 300);
    context.error(`GitHub returned ${res.status}: ${detail}`);
    return json({
      status: "error",
      message: res.status === 401 || res.status === 403
        ? "GitHub rejected the token - expired, revoked, or wrong scope"
        : `GitHub returned ${res.status}`,
    }, 502);
  },
});
