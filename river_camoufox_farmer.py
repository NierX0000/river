#!/usr/bin/env python3
"""
================================================================================
🌊 river_camoufox_farmer.py — Bulk signup River AI (cons:  console.river.ai)
    + auto API key, via Camoufox stealth (HEADLESS), temp-mail, dan proxy file.
================================================================================

Cara kerja
----------
River AI memakai WorkOS AuthKit (hosted) untuk signup — bukan endpoint REST.
Halaman signup: https://login.console.river.ai/sign-up  (client_id diambil dari
  GET https://console.river.ai/api/auth/config)
Turnstile "smart" + WorkOS Radar ada di halaman; Camoufox nyata dipakai agar
halaman berjalan seperti user asli (headless).

Alur per akun
  1. Buat inbox temp (Tempik)  ->  email
  2. Buka /sign-up via Camoufox (headless, +proxy) -> isi nama/email -> Continue
  3. Tunggu & baca kode/link verifikasi dari Tempik -> submit
  4. Selesaikan signup (set password bila diminta) -> redirect ke console
  5. Buka console.river.ai/keys -> buat & ambil API key (rv_...)
  6. Simpan ke accounts.jsonl + keys.txt

Dipakai (headless worker)
-------------------------
  python river_camoufox_farmer.py 5                  # 5 akun, headless
  python river_camoufox_farmer.py 10 -w 2            # 2 worker paralel (batched)
  python river_camoufox_farmer.py 10 -w 2 --proxy-file proxies.txt --batch-sleep 5
  python river_camoufox_farmer.py --headful 1        # tampilkan browser (debug)
  python river_camoufox_farmer.py --check            # cek konfig

Catatan
  - Turnstile "smart": di IP bersih (residential/direct) sering auto-solve di
    headless. Di proxy datacenter sering GAGAL -> pakai proxy residential.
  - Worker paralel di-batch: workers akun jalan, tunggu selesai, baru lanjut
    (agar tidak membebani IP/Turnstile).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import string
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_KEYS = os.path.join(HERE, "keys.txt")
OUT_LOG = os.path.join(HERE, "accounts.jsonl")
FILE_LOCK = threading.Lock()

# ── temp mail (Tempik) ──────────────────────────────────────────────
TEMPIK_API = os.getenv("TEMPIK_API", "https://tempik.mikhubs.my.id")
TEMPIK_DOM = os.getenv("TEMPIK_DOM", "mikhubs.my.id")
TEMP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# ── river.ai ────────────────────────────────────────────────────────
CONSOLE = "https://console.river.ai"
AUTH_CONFIG = f"{CONSOLE}/api/auth/config"
LOGIN_HOST = "https://login.console.river.ai"
KEYS_URL = f"{CONSOLE}/keys"

# proxy thread-local
_PROXY_LIST: list[str] = []
_PROXY_STATE = threading.local()


def log(idx, msg):
    print(f"  [{idx}] {msg}", flush=True)


# ─────────────────────────── proxy ───────────────────────────
def proxy_for_thread():
    if getattr(_PROXY_STATE, "px", "unset") != "unset":
        return _PROXY_STATE.px
    _PROXY_STATE.px = random.choice(_PROXY_LIST) if _PROXY_LIST else None
    return _PROXY_STATE.px


def reset_proxy_thread():
    _PROXY_STATE.px = "unset"


def camoufox_proxy(spec):
    """'user:pass@host:port' atau 'host:port' -> dict utk Camoufox."""
    if not spec:
        return None
    s = spec
    scheme = "http"
    for pre in ("http://", "https://", "socks5://"):
        if s.startswith(pre):
            scheme = pre.replace("://", "")
            s = s[len(pre):]
            break
    if "@" in s:
        cred, host = s.rsplit("@", 1)
        user, _, pwd = cred.partition(":")
        return {"server": f"{scheme}://{host}", "username": user, "password": pwd}
    return {"server": f"{scheme}://{s}"}


def load_proxies(path):
    items = []
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    items.append(line)
    _PROXY_LIST[:] = items
    return items


# ────────── pakai browser Camoufox yang SUDAH ada (no download) ──────────
def find_local_camoufox():
    """Cari binary Camoufox yang sudah ter-download di cache (~/Library/Caches/camoufox
    / ~/.cache/camoufox). Kembalikan path binary yg lengkap, atau None."""
    homes = [Path.home() / "Library" / "Caches" / "camoufox",
             Path.home() / ".cache" / "camoufox"]
    bins = []
    for cache in homes:
        base = cache / "browsers" / "official"
        if not base.exists():
            continue
        for d in base.iterdir():
            if not d.is_dir():
                continue
            # harus ada version.json (tanda install lengkap)
            if not (d / "version.json").exists():
                continue
            for cand in (
                d / "Camoufox.app" / "Contents" / "MacOS" / "camoufox",   # macOS
                d / "camoufox",                                          # linux
                d / "camoufox.exe",                                      # windows
            ):
                if cand.exists():
                    bins.append((d.stat().st_mtime, cand))
    if not bins:
        return None
    bins.sort(reverse=True)   # paling baru
    return str(bins[0][1])


# ─────────────────────────── temp mail ───────────────────────────
def _tempik_session():
    for i in range(5):
        try:
            req = urllib.request.Request(f"{TEMPIK_API}/api/session",
                                         headers={"User-Agent": TEMP_UA, "Accept": "application/json"})
            r = urllib.request.urlopen(req, timeout=20)
            sid = json.loads(r.read().decode()).get("sessionId")
            if sid:
                return sid
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


def tempik_inbox():
    sid = _tempik_session()
    if not sid:
        raise RuntimeError("Tempik: tidak dapat session")
    for i in range(5):
        lp = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        try:
            data = json.dumps({"localPart": lp, "domain": TEMPIK_DOM}).encode()
            req = urllib.request.Request(f"{TEMPIK_API}/api/inboxes", data=data, method="POST",
                                         headers={"x-session-id": sid, "Content-Type": "application/json",
                                                  "User-Agent": TEMP_UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status in (200, 201):
                    j = json.loads(r.read().decode())
                    return j["address"], sid
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    raise RuntimeError("Tempik: gagal buat inbox")


def tempik_read(addr, sid):
    try:
        req = urllib.request.Request(f"{TEMPIK_API}/api/inboxes/{addr}/messages",
                                     headers={"x-session-id": sid, "User-Agent": TEMP_UA,
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()) or []
    except Exception:
        return []


def poll_code(addr, sid, timeout=200):
    """Ambil kode 6 digit ATAU link verifikasi dari inbox tempik."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for m in tempik_read(addr, sid):
            blob = " ".join(str(m.get(k, "")) for k in ("subject", "body", "html", "text"))
            mm = re.search(r'(?<!\d)(\d{6})(?!\d)', blob)
            if mm:
                return ("code", mm.group(1))
            lm = re.search(r'(https?://[^\s"<>]*(?:confirm|verify|auth)[^\s"<>]*)', blob, re.I)
            if lm:
                return ("link", lm.group(1).replace("&amp;", "&"))
        time.sleep(2)
    return (None, None)


def newpass():
    return "Rv" + "".join(random.choices(string.ascii_letters + string.digits, k=10)) + "!x9"


# ─────────────────────────── core async ───────────────────────────
async def farm_async(idx, headless, proxy, email, sid, verbose=True):
    from camoufox.async_api import AsyncCamoufox

    def L(m):
        if verbose:
            log(idx, m)

    pwd = newpass()
    kw = {"headless": headless, "humanize": True}
    px = camoufox_proxy(proxy)
    if px:
        kw["proxy"] = px
        # geoip: samakan lokasi dgn proxy. Opsional — hanya jika paket geoip ada.
        try:
            import geoip2  # noqa: F401  (paket dari extra camoufox[geoip])
            kw["geoip"] = True
        except Exception:
            pass
    # PAKAI BROWSER YANG SUDAH ADA (cegah download ulang)
    exe = find_local_camoufox()
    if exe:
        kw["executable_path"] = exe
        log(idx, f"pakai browser lokal: {exe.split('/browsers/')[-1]}")
    else:
        log(idx, "browser lokal tidak ditemukan -> Camoufox akan mengunduh")

    async with AsyncCamoufox(**kw) as b:
        ctx = await b.new_context(ignore_https_errors=True,
                                  viewport={"width": 1280, "height": 950})
        page = await ctx.new_page()

        # 1) ambil client_id dari config
        try:
            await page.goto(AUTH_CONFIG, wait_until="domcontentloaded", timeout=60000)
            cfg = json.loads((await page.evaluate("()=>document.body.innerText")) or "{}")
        except Exception:
            cfg = {}
        cid = cfg.get("workos_client_id")
        if not cid:
            cfg = _fetch_config(proxy)
            cid = cfg.get("workos_client_id")
        if not cid:
            return "config_failed", "tidak dapat workos_client_id", ""

        # 1) buka lewat SPA console (PENTING utk PKCE: SPA simpan code_verifier,
        #    lalu redirect sendiri ke login.console.river.ai). Membuka AuthKit
        #    langsung -> code_verifier tak tersimpan -> callback 401.
        L("buka console.river.ai/sign-up (SPA -> redirect ke AuthKit)...")
        got = False
        for attempt in range(3):
            try:
                await page.goto(f"{CONSOLE}/sign-up", wait_until="domcontentloaded", timeout=60000)
                got = True
                break
            except Exception as e:
                es = str(e)
                L(f"goto gagal (attempt {attempt+1}/3): {es[:60]}")
                if "NET_TIMEOUT" in es or "NS_ERROR" in es or "timeout" in es.lower():
                    await page.wait_for_timeout(3000)
                    continue
                break
        if not got:
            return "proxy_timeout", "halaman signup tidak bisa dibuka (proxy lambat/mati)", ""

        # tunggu sampai berpindah ke login.console.river.ai & form muncul
        t0 = time.time()
        on_authkit = False
        while time.time() - t0 < 60:
            await page.wait_for_timeout(2000)
            try:
                host = urllib.parse.urlparse(page.url).hostname or ""
            except Exception:
                host = ""
            if "login.console.river.ai" in host:
                on_authkit = True
                break
            # kalau sudah ada form (mungkin di-render di host console)
            if await page.locator("input[name='email']").count() > 0:
                on_authkit = True
                break
        if not on_authkit:
            await _dump_debug(page, idx)
            return "authkit_not_reached", "tidak sampai ke halaman AuthKit", ""
        L("di halaman AuthKit")
        await page.wait_for_timeout(3000)

        # 2) isi form (verifikasi tiap field benar-benar terisi)
        try:
            ok1 = await _fill(page, "input[name='first_name']", "Boz")
            ok2 = await _fill(page, "input[name='last_name']", random.choice(["Agent", "User", "Dev"]))
            ok3 = await _fill(page, "input[name='email']", email)
            if not (ok1 and ok3):
                await _dump_debug(page, idx)
                return "form_fill_failed", f"field tidak terisi (first={ok1} email={ok3})", ""
        except Exception as e:
            return "form_fill_failed", f"{type(e).__name__}: {str(e)[:80]}", ""
        L("form terisi")
        await page.wait_for_timeout(1500)  # beri waktu Turnstile mulai

        # 3) klik Continue
        await _click(page, "button:has-text('Continue')")
        L("Continue dikirim, menunggu Turnstile solve...")

        # 3b) tunggu Turnstile: bot_detection_token terisi + halaman berpindah
        solved = await _wait_turnstile(page, timeout=60)
        if solved:
            L("Turnstile ter-solve")
        else:
            L("Turnstile belum ter-solve dalam 60s (proxy mungkin ditolak Cloudflare)")

        # 4) tunggu layar berikutnya (password / kode / magic / dashboard)
        state = await _wait_next_screen(page, idx, L, timeout=90)
        L(f"layar: {state}")

        # 4b) kalau masih di form signup -> Turnstile gagal / signup ditolak
        if state == "timeout":
            await _dump_debug(page, idx)
            # cek pesan error di halaman
            err = await _page_error(page)
            return "turnstile_blocked", err or "Turnstile/signup tidak lanjut (layar timeout)", ""

        # 5) tangani password bila diminta
        if state == "password":
            try:
                # isi semua field password yg ada
                pws = await page.query_selector_all("input[type='password']")
                for i in range(len(pws)):
                    loc = page.locator("input[type='password']").nth(i)
                    try:
                        await loc.click(timeout=6000)
                        await loc.fill("")
                        await loc.press_sequentially(pwd, delay=45)
                    except Exception:
                        pass
                # klik tombol submit
                await _click(page, "button:has-text('Continue'), button:has-text('Create account'), button:has-text('Create'), button[type='submit']")
                L("password dikirim, tunggu layar berikutnya...")
                # poll cepat sampai layar berubah (maks ~15s)
                for _ in range(15):
                    await page.wait_for_timeout(1000)
                    state = await _detect_screen(page)
                    if state not in ("password", "unknown"):
                        break
                L(f"setelah password -> {state}")
                # WorkOS biasanya kirim kode verifikasi email setelah password
                if state in ("password", "unknown", "verify", "code"):
                    L("cek inbox untuk kode verifikasi email...")
                    kind, val = poll_code(email, sid, timeout=90)
                    if val:
                        L(f"kode verifikasi diterima ({kind})")
                        if kind == "code":
                            try:
                                await _fill(page, "input[inputmode='numeric'], input[name*='code'], input[autocomplete='one-time-code']", val)
                                await _click(page, "button:has-text('Continue'), button:has-text('Verify'), button[type='submit']")
                                for _ in range(15):
                                    await page.wait_for_timeout(1000)
                                    state = await _detect_screen(page)
                                    if state not in ("password", "unknown", "code"):
                                        break
                                L(f"setelah kode -> {state}")
                            except Exception as e:
                                L(f"gagal isi kode: {str(e)[:60]}")
            except Exception as e:
                return "password_step_failed", str(e)[:100], ""

        # 6) kalau minta kode verifikasi email
        if state in ("verify", "code", "email"):
            kind, val = poll_code(email, sid, timeout=200)
            if not val:
                return "no_code", "kode/verifikasi tidak sampai ke inbox", ""
            L(f"verifikasi ({kind}) diterima")
            if kind == "code":
                try:
                    await _fill(page, "input[inputmode='numeric'], input[name*='code'], input[autocomplete='one-time-code']", val)
                    await _click(page, "button:has-text('Continue'), button:has-text('Verify'), button[type='submit']")
                except Exception:
                    try:
                        await page.keyboard.type(val)
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
            else:
                try:
                    await page.goto(val, wait_until="domcontentloaded", timeout=90000)
                except Exception:
                    pass
            await page.wait_for_timeout(6000)
            state = await _detect_screen(page)
            L(f"setelah verifikasi -> {state}")

        # 7) tunggu sampai masuk console
        ok = await _wait_console(page, idx, L, timeout=120)
        if not ok:
            body = (await page.evaluate("()=>document.body.innerText")) or ""
            return "no_session", body[:150], ""

        # 8) buat & ambil API key dari /keys
        key = await _mint_key(page, idx, L)
        if not key:
            return "key_not_found", "tidak menemukan API key di console", ""
        return "ok", "", key


def _fetch_config(proxy=None):
    try:
        px = camoufox_proxy(proxy)
        hdrs = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Accept": "application/json"}
        req = urllib.request.Request(AUTH_CONFIG, headers=hdrs)
        if px:
            surl = px["server"]
            if px.get("username"):
                surl = surl.replace("://", f"://{px['username']}:{px['password']}@")
            op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": surl, "https": surl}))
            with op.open(req, timeout=25) as r:
                return json.loads(r.read().decode())
        else:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
    except Exception:
        return {}


async def _fill(page, sel, val, tries=3):
    """Isi field dgn retry + verifikasi nilai benar-benar masuk."""
    for t in range(tries):
        try:
            await page.wait_for_selector(sel, timeout=15000, state="visible")
            loc = page.locator(sel).first
            await loc.click(timeout=8000)
            await loc.fill("")
            await loc.press_sequentially(val, delay=45)
            # verifikasi
            got = await loc.input_value(timeout=5000)
            if got and got.strip() == val:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(1500)
    # terakhir: coba via JS setter (React-friendly)
    try:
        ok = await page.evaluate("""(a)=>{
            const el = document.querySelector(a.sel);
            if(!el) return false;
            const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
            el.focus(); set.call(el, a.val);
            el.dispatchEvent(new Event('input',{bubbles:true}));
            el.dispatchEvent(new Event('change',{bubbles:true}));
            return el.value===a.val;
        }""", {"sel": sel, "val": val})
        return bool(ok)
    except Exception:
        return False


async def _wait_turnstile(page, timeout=60):
    """Tunggu hidden field bot_detection_token terisi (Turnstile solved)
    ATAU halaman berpindah dari form signup."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        await page.wait_for_timeout(1000)
        try:
            r = await page.evaluate("""() => {
                const b = document.querySelector("input[name='bot_detection_token']");
                const url = location.href;
                const leftForm = !document.querySelector("input[name='email']") || /password|verify|code|success|console/.test(url);
                return {token: b ? (b.value||'').length : -1, left: leftForm};
            }""")
            if r.get("token", 0) > 0 or r.get("left"):
                return True
        except Exception:
            pass
    return False


async def _page_error(page):
    try:
        return await page.evaluate("""() => {
            const el = [...document.querySelectorAll('[role=alert], .error, [aria-live]')]
                .map(e=>(e.innerText||'').trim()).filter(Boolean);
            const t = (document.body.innerText||'');
            const m = t.match(/(invalid|error|failed|not allowed|too many|blocked|verify your email|enter your email)[^\\n]{0,60}/i);
            return (el[0] || (m?m[0]:'') || '').slice(0,180);
        }""")
    except Exception:
        return ""


async def _dump_debug(page, idx):
    """Simpan screenshot + HTML saat gagal, untuk diagnosa."""
    try:
        d = os.path.join(HERE, "debug")
        os.makedirs(d, exist_ok=True)
        await page.screenshot(path=os.path.join(d, f"fail_{idx}.png"), full_page=True)
        html = await page.content()
        with open(os.path.join(d, f"fail_{idx}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        log(idx, f"debug disimpan: debug/fail_{idx}.png + .html")
    except Exception:
        pass


async def _click(page, sel):
    try:
        await page.click(sel, timeout=6000)
    except Exception:
        label = sel.split("'")[1] if "'" in sel else "continue"
        await page.evaluate("""(s)=>{const b=[...document.querySelectorAll('button,[role=button]')]
            .find(e=>new RegExp(s,'i').test(e.innerText||''));if(b)b.click();}""", label)


async def _detect_screen(page):
    try:
        return await page.evaluate("""() => {
            const has = s => !!document.querySelector(s);
            const t = (document.body.innerText||'').toLowerCase();
            if (has("input[type='password']")) return 'password';
            if (has("input[inputmode='numeric']") || has("input[autocomplete='one-time-code']")) return 'code';
            if (/verify|verification|check your (inbox|email)|enter the code/.test(t)) return 'verify';
            if (/keys|api key|workspace|sign out|logout/i.test(t)) return 'console';
            return 'unknown';
        }""")
    except Exception:
        return "unknown"


async def _wait_next_screen(page, idx, L, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        await page.wait_for_timeout(1000)
        s = await _detect_screen(page)
        if s != "unknown":
            return s
    return "timeout"


async def _wait_console(page, idx, L, timeout=150):
    """Tunggu sampai masuk console.river.ai (HOST TEPAT, bukan login.console.river.ai).
    Tahan terhadap NS_ERROR_ABORT (navigasi saat halaman transisi)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        await page.wait_for_timeout(1000)
        try:
            host = urllib.parse.urlparse(page.url).hostname or ""
            path = urllib.parse.urlparse(page.url).path or ""
        except Exception:
            host, path = "", ""
        if host == "console.river.ai" and "/auth/" not in path and "login" not in path:
            return True
        # coba buka /keys dengan retry (NS_ERROR_ABORT bisa terjadi saat transisi)
        for attempt in range(3):
            try:
                await page.goto(KEYS_URL, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1500)
                host = urllib.parse.urlparse(page.url).hostname or ""
                path = urllib.parse.urlparse(page.url).path or ""
                if host == "console.river.ai" and "/login" not in path and "/auth/" not in path:
                    return True
                break
            except Exception as e:
                if "NS_ERROR_ABORT" in str(e) or "interrupted" in str(e).lower():
                    await page.wait_for_timeout(1500)  # tunggu transisi selesai
                    continue
                break
    return False


async def _mint_key(page, idx, L):
    # buka /keys dgn retry (hindari NS_ERROR_ABORT)
    for attempt in range(3):
        try:
            await page.goto(KEYS_URL, wait_until="domcontentloaded", timeout=60000)
            break
        except Exception as e:
            if "NS_ERROR_ABORT" in str(e) or "interrupted" in str(e).lower():
                await page.wait_for_timeout(3000)
                continue
            break
    await page.wait_for_timeout(1200)

    # 0) Pastikan halaman /keys sudah siap (user login terlihat di sidebar)
    await page.wait_for_timeout(800)
    try:
        txt0 = (await page.evaluate("()=>document.body.innerText")) or ""
    except Exception:
        txt0 = ""
    if "API keys" not in txt0 and "API Keys" not in txt0:
        # mungkin belum render / masih transisi -> tunggu
        await page.wait_for_timeout(1500)

    # 1) KLIK "+ Create Key" (UI) — key dibuat lewat tombol ini
    L("klik '+ Create Key'...")
    clicked = False
    for sel in ("button:has-text('Create Key')",
                "button:has-text('Create key')",
                "button:has-text('Create your first API key')",
                "button:has-text('Create')"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=6000)
                clicked = True
                L(f"klik tombol: {sel}")
                break
        except Exception:
            continue
    if not clicked:
        # coba via JS cari button yang mengandung 'create'
        clicked = await page.evaluate("""()=>{
            const b=[...document.querySelectorAll('button')]
              .find(e=>/create\\s*(key|your first)/i.test(e.innerText||''));
            if(b){b.click();return true;} return false;
        }""")
        if clicked:
            L("klik tombol Create (via JS)")
    # poll cepat sampai modal/daftar berubah (maks 8s)
    for _ in range(8):
        await page.wait_for_timeout(700)
        try:
            t = (await page.evaluate("()=>document.body.innerText")) or ""
            if "name" in t.lower() or "API key" in t or "rv_" in t:
                break
        except Exception:
            pass

    # 2) mungkin muncul modal: isi nama (opsional) lalu submit
    try:
        if await page.locator("input[type='text'], input[name='name'], input[placeholder*='name' i]").count() > 0:
            await _fill(page, "input[type='text'], input[name='name'], input[placeholder*='name' i]",
                        f"9Router-{random.randint(1000,9999)}")
            for b in ("button:has-text('Create')", "button:has-text('Create Key')", "button[type='submit']"):
                try:
                    loc = page.locator(b).first
                    if await loc.count() > 0:
                        await loc.click(timeout=5000)
                        break
                except Exception:
                    continue
    except Exception:
        pass

    # 3) key biasanya muncul SEKALI di layar/modal setelah dibuat -> baca (poll cepat)
    for _ in range(12):
        txt = (await page.evaluate("()=>document.body.innerText")) or ""
        key = _extract_rv_key(txt)
        if key:
            return key
        # cek input readonly yang berisi key
        v = await page.evaluate("""()=>{
            const i=[...document.querySelectorAll('input,code,textarea,pre')]
              .map(e=>e.value||e.innerText||'').find(s=>/rv_[A-Za-z0-9_-]{12,}/.test(s));
            if(!i) return '';
            const m=String(i).match(/rv_[A-Za-z0-9_-]{12,}/); return m?m[0]:'';
        }""")
        if v:
            return v
        await page.wait_for_timeout(1000)

    # 4) fallback API: POST /api/keys (sesi browser)
    L("fallback: POST /api/keys...")
    for attempt in range(3):
        res = await page.evaluate("""async () => {
            const body = {name: "9Router-" + Math.random().toString(36).slice(2,8)};
            try {
                const r = await fetch("/api/keys", {method:"POST", credentials:"include",
                    headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
                return {status: r.status, body: (await r.text()).slice(0, 1500)};
            } catch(e) { return {status:-1, body:String(e)}; }
        }""")
        L(f"POST /api/keys -> HTTP {res.get('status')}: {str(res.get('body',''))[:150]}")
        key = _extract_rv_key(res.get("body", ""))
        if key:
            return key
        if res.get("status") in (401, 403) and attempt < 2:
            await page.wait_for_timeout(4000)
            continue
        break

    # 5) fallback GET /api/keys -> ambil key yang ada
    res = await page.evaluate("""async () => {
        try { const r = await fetch("/api/keys", {credentials:"include"});
              return {status:r.status, body:(await r.text()).slice(0,3000)}; }
        catch(e){ return {status:-1, body:String(e)}; }
    }""")
    L(f"GET /api/keys -> HTTP {res.get('status')}: {str(res.get('body',''))[:150]}")
    key = _extract_rv_key(res.get("body", ""))
    if key:
        return key

    # 6) terakhir: reload & baca DOM
    try:
        await page.goto(KEYS_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
        txt = (await page.evaluate("()=>document.body.innerText")) or ""
        key = _extract_rv_key(txt)
        if key:
            return key
    except Exception:
        pass
    return None


def _extract_rv_key(s):
    if not isinstance(s, str):
        return None
    m = re.search(r'\b(rv_[A-Za-z0-9_\-]{12,})\b', s)
    return m.group(1) if m else None


# ─────────────────────────── worker ───────────────────────────
def farm_one(idx, headless, email=None, sid=None, verbose=True):
    reset_proxy_thread()
    proxy = proxy_for_thread()
    rec = {"index": idx, "status": "failed", "email": "", "api_key": "",
           "error": "", "proxy": proxy or "direct"}
    try:
        if not email:
            email, sid = tempik_inbox()
        rec["email"] = email
        if verbose:
            log(idx, f"email: {email}")
        status, err, key = asyncio.run(farm_async(idx, headless, proxy, email, sid, verbose))
        if status == "ok" and isinstance(key, str) and key.startswith("rv_"):
            rec["status"], rec["api_key"] = "success", key
            with FILE_LOCK:
                with open(OUT_KEYS, "a", encoding="utf-8") as f:
                    f.write(key + "\n")
            if verbose:
                log(idx, f"✔ KEY: {key[:22]}...")
        else:
            rec["error"] = f"{status}: {err or ('key tidak valid: ' + str(key)[:60])}"
            if verbose:
                log(idx, f"✖ {status} — {rec['error'][:110]}")
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:130]}"
        if verbose:
            log(idx, f"✖ EXC {rec['error'][:100]}")

    with FILE_LOCK:
        with open(OUT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    return rec


# ─────────────────────────── CLI ───────────────────────────
def cmd_check(proxy_file):
    px = load_proxies(proxy_file)
    print("Konfigurasi:")
    print(f"  Console      : {CONSOLE}")
    print(f"  Tempik       : {TEMPIK_API} @ {TEMPIK_DOM}")
    print(f"  Proxy file   : {proxy_file or '(tidak ada)'} -> {len(px)} proxy")
    cfg = _fetch_config(px[0] if px else None)
    print(f"  workos_client: {cfg.get('workos_client_id')}")
    print(f"  password_sigp: {cfg.get('password_signup')}  password_login: {cfg.get('password_login')}")
    print("  (signup lewat WorkOS hosted AuthKit + Turnstile — perlu Camoufox)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="River AI bulk signup + API key (Camoufox headless)")
    ap.add_argument("count", nargs="?", type=int, default=1)
    ap.add_argument("-w", "--workers", type=int, default=1)
    ap.add_argument("--batch-sleep", type=float, default=0)
    ap.add_argument("--proxy", default=None, help="proxy tunggal (user:pass@host:port)")
    ap.add_argument("--proxy-file", default=None, help="file daftar proxy (1/baris)")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    proxy_file = args.proxy_file
    if args.proxy:
        # proxy tunggal
        _PROXY_LIST[:] = [args.proxy]
        print(f"  [proxy] 1 proxy (dari --proxy)")
    else:
        # default: baca proxies.txt di folder script bila ada
        if not proxy_file:
            default_pf = os.path.join(HERE, "proxies.txt")
            if os.path.exists(default_pf):
                proxy_file = default_pf
        px = load_proxies(proxy_file)
        if px:
            print(f"  [proxy] {len(px)} proxy dari {os.path.basename(proxy_file)}")

    if args.check:
        return cmd_check(proxy_file)

    n = args.count or 1
    headless = not args.headful
    print(f"River AI Farmer | {n} akun | workers {args.workers} | "
          f"{'headful' if args.headful else 'headless'} | proxy {len(_PROXY_LIST)}")

    ok = 0
    if args.workers > 1:
        start = 1
        while start <= n:
            batch = list(range(start, min(start + args.workers, n + 1)))
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(farm_one, i, headless): i for i in batch}
                for f in as_completed(futs):
                    if f.result().get("status") == "success":
                        ok += 1
            if start + args.workers <= n and args.batch_sleep:
                print(f"  [batch] selesai {len(batch)} akun, cooldown {args.batch_sleep}s...", flush=True)
                time.sleep(args.batch_sleep)
            start += args.workers
    else:
        for i in range(1, n + 1):
            if farm_one(i, headless).get("status") == "success":
                ok += 1

    print(f"\n=== {ok}/{n} berhasil -> {OUT_KEYS} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
