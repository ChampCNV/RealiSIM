# RealiSIM

From your video to simulation-ready assets and environments.

RealiSIM turns a phone video of a tabletop workspace or a single object into simulation-ready 3D assets and a MuJoCo scene, exported as a Gymnasium environment. Built at Cal Hacks 13.0 on Mana (mana.space).

## How it works
1. **Upload a video** (resumable, SHA-256 chunked upload).
2. **Autofill with Claude**: Claude vision proposes objects, dimensions, mass, friction and material. Every value is labelled Measured, Supplied, Estimated or Unknown; you review before building. Optional web evidence via a private Apify Actor (`apify-actor/`).
3. **Build**: BiRefNet cutouts, TRELLIS meshes (GPU), CoACD collision hulls, robot models from MuJoCo Menagerie (multi-arm supported).
4. **Simulate and export**: server-side MuJoCo drop test, then a pip-installable Gymnasium package checked in a clean environment before download.

## Layout
- `backend/` aiohttp API: pipeline worker (parallel, one job per project), workflow, public beta isolation and caps, visitor metrics, Claude copilot, Apify item evidence, release gate.
- `adapter/` bridge to the upstream photo2grasp pipeline: workcell composition, layout, Gymnasium export, remote preprocessing.
- `frontend/` React app pieces (assembled by `deploy-ui.py`; `build/realisim-app.jsx` is the current build) and the product-only public shell.
- `spacetimedb/module/` SpacetimeDB module: system of record for projects, runs, versions and artifact bytes.
- `apify-actor/` private Apify Actor `realisim-item-evidence`.
- `support/` Factory Droid SDK troubleshooting worker. `ops/` scheduled maintenance, disk guard, auto-update config. `tests/` focused test suites.

## Upstream
Built on the photo2grasp research repository (github.com/adityabangde/photo2grasp), tracked at commit 166a8ed and not vendored here. Review licenses of upstream code, robot models and generated assets before redistribution.

## Notes
No secrets are stored in this repository; credentials are injected at runtime by Mana.
