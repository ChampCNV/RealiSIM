
import sys, aiohttp
from aiohttp import web
H = "/workspace/photo2grasp/harness"; BACK = "http://127.0.0.1:" + open("/workspace/photo2grasp/backend/port").read().strip()
UID = {"uid": sys.argv[2] if len(sys.argv) > 2 else "9fK1vhrVDLg53aTW10xgWPHK7Zt2"}
async def index(r): return web.FileResponse(H + "/index.html")
async def js(r): return web.FileResponse(H + "/app.js", headers={"Content-Type": "application/javascript"})
async def setuid(r): UID["uid"] = r.query["uid"]; return web.Response(text="ok")
async def proxy(r):
    path = r.match_info["tail"]
    async with aiohttp.ClientSession() as s:
        async with s.request(r.method, BACK + "/" + path + ("?" + r.query_string if r.query_string else ""), data=await r.read(),
                             headers={"X-Sender-Uid": UID["uid"], "Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
                                      "X-Chunk-Sha256": r.headers.get("X-Chunk-Sha256", "")}) as resp:
            body = await resp.read()
            return web.Response(body=body, status=resp.status, headers={"Content-Type": resp.headers.get("Content-Type", "application/json")})
app = web.Application(client_max_size=64 * 2**20)
app.router.add_get("/", index); app.router.add_get("/app.js", js); app.router.add_get("/setuid", setuid)
app.router.add_route("*", "/b/{tail:.*}", proxy)
web.run_app(app, host="127.0.0.1", port=int(sys.argv[1]), print=None)
