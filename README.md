# The Gridiron Gazette

Type in a Sleeper username and get a start/sit recommendation for every fantasy football league that user is in,
with the reasoning for each player, waiver pickups, trade ideas and a look at the coming weeks.

Projections start from Sleeper's own numbers and are adjusted slightly by a model that weighs this season's form,
team performance, depth chart and injuries, matchups and game script. If a visitor supplies an Anthropic API key,
Claude also checks the latest news and makes the final lineup call.

## What is in this folder

```
public/            the website (index.html, app.js, site.css, app.css)
api/leagues.py     GET /api/leagues?username=...      lists the user's leagues (fast)
api/lineup.py      GET /api/lineup?username=&league_id=   analyzes one league (the slow call)
lib/               shared server helpers (validation, rate limits, key handling)
fantasy.py         the engine (also runs on its own: python fantasy.py)
dev_server.py      run the whole site on your own computer
scripts/           build_css.py regenerates public/app.css from the engine
vercel.json        Vercel settings (Fluid compute, 5 minute limit, security headers)
```

## Try it on your computer first

```
pip install -r requirements.txt
python dev_server.py
```

Open http://localhost:3000 and enter a Sleeper username.

## Put it on GitHub

1. Create a free account at github.com, then click **New repository**. Name it, for example, `gridiron-gazette`.
   Leave "Add a README" unchecked. Public or private both work with Vercel.
2. Open a Command Prompt in this folder and run (replace YOUR-USERNAME):

```
git init
git add .
git commit -m "First version"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/gridiron-gazette.git
git push -u origin main
```

3. Check that no key file was uploaded: `git ls-files` should not list `anthropic_key.txt` or anything like it.
   The `.gitignore` already blocks those names. If a key ever gets pushed, delete it in the Anthropic console
   and create a new one, because anything pushed to GitHub should be treated as public.

## Put it live on Vercel

1. Go to vercel.com and sign up with your GitHub account.
2. Click **Add New, Project**, choose your repository, and click **Import**.
3. Set **Framework Preset** to **Other**. Leave the build command and output directory empty.
4. Click **Deploy**. When it finishes, Vercel gives you a web address. Open it and test with your username.
5. Every `git push` to `main` redeploys automatically.

Fluid compute must be on so a function can run up to 5 minutes. It is on by default for new projects, and
`vercel.json` also asks for it. If a league analysis ends in a timeout, check Project Settings, Functions.

## Optional settings (Project Settings, Environment Variables)

| Name | What it does |
| --- | --- |
| `ENABLE_SERVER_CLAUDE` = `1` and `ANTHROPIC_API_KEY` = your key | Lets visitors use Claude's final call on YOUR key. This costs you money, so only turn it on if you accept that. Each visitor is limited to `SERVER_CLAUDE_PER_DAY` league analyses (default 2). |
| `FANTASY_SLEEPER_PROJECTIONS` = `0` | Do not use Sleeper's undocumented projection feed; the model's own numbers are used. |
| `FANTASY_IMAGES` = `0` | Show plain team chips instead of loading player photos and logos from Sleeper's servers. |
| `FANTASY_WAIVER_POOL`, `FANTASY_BUY_LOW_COUNT`, `FANTASY_LEARN_WEEKS` | Make each analysis lighter (faster and cheaper) or heavier. |

Without any settings, visitors get the projection model's picks, and anyone can paste their own Anthropic key on
the page to turn Claude on for their own request. That key is sent only with the request, held only in memory
while it runs, and never stored or logged.

## Good to know

- **First visit after a quiet spell is slow.** The server loads NFL data and Sleeper's weekly feeds before
  the first league, which can take a minute. Later leagues, and later visitors on the same warm server, are quicker.
- **Rate limits are best-effort.** They are kept in memory per server instance, so they stop accidents and casual
  abuse but are not a hard guarantee. For strict limits, store counters in a shared service such as Upstash Redis.
- **Vercel's Hobby plan is for personal, non-commercial use.** Check Vercel's terms if the site grows or earns money.
- **Preview URLs** (the ones Vercel makes for each branch) are protected by default. Your production address is public.
- **Sleeper:** its official API covers rosters, leagues and players. The projection and weekly stat feeds this
  project uses are undocumented and could change or disappear, and Sleeper's terms limit how its data, photos and
  logos may be used. Fine for personal use; read their terms before promoting a public site, and consider the two
  switches above. If the feeds fail, the app still works with the model's own numbers and says so on the page.
- Nothing here is betting or financial advice. Projections are estimates.

## Changing how it looks or works

- Styles for the results live in `fantasy.py` (the `CSS` block). After editing, run `python scripts/build_css.py`
  to refresh `public/app.css`, then commit both files.
- All model weights are in the `W` dictionary near the top of `fantasy.py`.
- To run it as before on your own computer, set `SLEEPER_USERNAME` at the top of `fantasy.py` and run `python fantasy.py`.
