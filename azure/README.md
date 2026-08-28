# Azure Function — refresh trigger

Starts the "Refresh stock data" workflow on GitHub when the board's Refresh
button is pressed. It holds the GitHub token so the page doesn't have to.

## Deploy (you already have the Core Tools installed)

**1. Create the Function App** — portal.azure.com → Create a resource →
Function App → **Consumption** plan, runtime **Node.js 20**, region UK South.
At this volume it costs effectively nothing (the free grant covers ~1M
executions/month).

**2. Make the token** at github.com/settings/personal-access-tokens/new

- Repository access: *Only select repositories* → `Production-Build`
- Permissions: **Actions = Read and write**. Nothing else.
- Note the expiry — when it lapses the button fails and nothing else does.

**3. Add the app settings** — Function App → Settings → Environment variables:

| Name | Value |
|---|---|
| `GH_TOKEN` | the token from step 2 |
| `GH_REPO` | `DavidSweeneyOwen/Production-Build` |
| `GH_BRANCH` | `main` |

**4. Allow the board to call it** — Function App → API → CORS → add
`https://davidsweeneyowen.github.io`. Nothing works without this.

**5. Deploy**, from this `azure` folder:

```cmd
npm install
func azure functionapp publish YOUR-FUNCTION-APP-NAME
```

**6. Get the URL** — Function App → Overview → Functions → `refresh` → Get
Function Url. It looks like
`https://your-app.azurewebsites.net/api/refresh?code=abc123...`
Copy the whole thing, `?code=` and all.

**7. Wire up the board** — in `docs/index.html`, leave `GH_PAT` empty and paste
that URL into `TRIGGER_URL`. Commit and push. Push protection won't complain:
a function URL is not a credential in the way a PAT is, and the worst anyone
can do with it is start a workflow run.

## Check it works

```cmd
curl -X POST "https://your-app.azurewebsites.net/api/refresh?code=abc123..."
```

- `{"status":"queued"}` — working, a run should appear in Actions
- `{"status":"throttled"}` — also working, just too soon after the last run
- `"Function is not configured"` — `GH_TOKEN` or `GH_REPO` didn't save
- `"GitHub rejected the token"` — expired, revoked, or missing Actions write

If the button fails in the browser but curl works, it's CORS — step 4.
