"""v0.6.0: sidebar entry of the deleted chat is hidden (with retries) by the browser script."""
import shutil, subprocess, textwrap
import pytest
import model_handoff as mh

HARNESS = textwrap.dedent("""
const mode = process.argv[2];
const realST = setTimeout;
global.setTimeout = (f) => realST(f, 0);
const log = {fetches: [], infos: [], warns: []};
console.info = (...a) => log.infos.push(a.join(' '));
console.warn = (...a) => log.warns.push(a.join(' '));
global.localStorage = {token: 't'};
global.sessionStorage = {setItem() {}};
global.location = {pathname: '/'};
function mkEl(attrs = {}) { return {style: {}, tagName: 'DIV', children: [], parentElement: null, ...attrs}; }
const nav = mkEl({tagName: 'NAV'});
const row = mkEl(); const other = mkEl();
const link = mkEl({tagName: 'A', href: '/c/OLD'}); const otherLink = mkEl({tagName: 'A', href: '/c/OTHER'});
row.parentElement = nav; link.parentElement = row; other.parentElement = nav; otherLink.parentElement = other;
row.querySelectorAll = () => [link]; other.querySelectorAll = () => [otherLink];
nav.querySelectorAll = () => [link, otherLink];
let reappear = 0;
const editor = {innerText: 'hello'};
const btn = {disabled: false, click() { location.pathname = '/c/NEW'; }};
global.document = {
  body: {appendChild() {}},
  createElement() { return {click() {}, remove() {}}; },
  getElementById(id) { return id === 'chat-input' ? editor : btn; },
  querySelectorAll(sel) {
    if (mode === 'missing') return [];
    if (sel === 'a[href="/c/OLD"]') {
      // simulate the list re-rendering and restoring the entry once
      if (mode === 'rerender' && ++reappear === 3) row.style = {};
      return [link];
    }
    return [];
  },
};
global.fetch = async (url, opt) => {
  log.fetches.push([url, opt.method]);
  return mode === 'fail' ? {ok: false, status: 500} : {ok: true, status: 200};
};
const js = process.argv[3];
const fn = new Function(js);
fn();
realST(() => { console.log(JSON.stringify({log, hidden: row.style.display === 'none',
  otherHidden: other.style.display === 'none'})); }, 1500);
""")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def run(mode, tmp_path):
    js = mh.build_js("m", "hello", [], True, delete_chat_id="OLD")
    h = tmp_path / "h.js"
    h.write_text(HARNESS)
    out = subprocess.run(["node", str(h), mode, js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    import json
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_syntax(tmp_path):
    js = mh.build_js("m", "hello", [], True, delete_chat_id="OLD")
    f = tmp_path / "s.js"
    f.write_text("(async()=>{" + js + "})")
    assert subprocess.run(["node", "--check", str(f)]).returncode == 0


def test_deleted_and_entry_hidden(tmp_path):
    r = run("ok", tmp_path)
    assert r["log"]["fetches"] == [["/api/v1/chats/OLD", "DELETE"]]
    assert r["hidden"] and not r["otherHidden"]


def test_rehidden_after_rerender(tmp_path):
    assert run("rerender", tmp_path)["hidden"]


def test_failed_delete_leaves_sidebar_alone(tmp_path):
    r = run("fail", tmp_path)
    assert not r["hidden"]
    assert any("500" in w for w in r["log"]["warns"])


def test_missing_entry_logs_info(tmp_path):
    r = run("missing", tmp_path)
    assert any("not found in sidebar" in i for i in r["log"]["infos"])


def test_no_cleanup_without_chat_id():
    assert "dropFromSidebar(oldChat)" in mh.build_js("m", "x", [], True, "OLD")
    assert 'const oldChat = "";' in mh.build_js("m", "x", [], True, "")
