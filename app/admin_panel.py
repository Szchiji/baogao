import html
import json
from typing import Any

from app.crud import setting_get
from app.keyboards import report_template
from app.utils import parse_json

# Tabs that are only shown to the main admin; child-bot sub-admins cannot see these.
_MAIN_ADMIN_ONLY_TABS = frozenset(
    {"basic", "welcome", "keyboard", "template", "texts", "review", "broadcast", "child-bots"}
)

def report_to_html(report_row: dict) -> str:
    data = parse_json(report_row["data_json"], {})
    bot_id = report_row.get("bot_id", "")
    tpl = report_template(bot_id=bot_id)
    field_labels = {f["key"]: f["label"] for f in tpl.get("fields", [])}
    field_types = {f["key"]: f.get("type", "text") for f in tpl.get("fields", [])}

    status = report_row.get("status", "")
    status_map = {
        "pending": ("<span style='background:rgba(245,158,11,.15);color:#fde68a;border:1px solid rgba(245,158,11,.3);padding:2px 10px;border-radius:12px;font-size:.8rem;font-weight:600'>⏳ 待审核</span>",),
        "approved": ("<span style='background:rgba(16,185,129,.15);color:#6ee7b7;border:1px solid rgba(16,185,129,.3);padding:2px 10px;border-radius:12px;font-size:.8rem;font-weight:600'>✅ 已通过</span>",),
        "rejected": ("<span style='background:rgba(244,63,94,.15);color:#fca5a5;border:1px solid rgba(244,63,94,.3);padding:2px 10px;border-radius:12px;font-size:.8rem;font-weight:600'>❌ 已驳回</span>",),
    }
    status_badge = status_map.get(status, (html.escape(status),))[0]

    rows_html = ""
    seen: set[str] = set()
    ordered_keys = [f["key"] for f in tpl.get("fields", [])]
    for k in ordered_keys + [k for k in data if k not in ordered_keys]:
        if k in seen or k not in data:
            continue
        seen.add(k)
        label = html.escape(field_labels.get(k, k))
        if field_types.get(k, "text") == "photo":
            val_html = "<span style='color:#8b95b0;font-style:italic'>📷 图片字段</span>"
        else:
            val_html = html.escape(str(data[k]))
        rows_html += f"""
        <div style='margin-bottom:16px'>
          <div style='font-size:.7rem;font-weight:700;color:#5a6480;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px'>{label}</div>
          <div style='font-size:.93rem;color:#dde2ed;line-height:1.6;white-space:pre-wrap;word-break:break-word'>{val_html}</div>
        </div>"""

    created_at = html.escape(str(report_row.get("created_at", ""))[:19])
    username = html.escape(report_row.get("username") or "unknown")
    report_id = html.escape(str(report_row.get("id", "")))

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>报告 #{report_id}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#070912;background-image:radial-gradient(ellipse 80% 50% at 20% 40%,rgba(99,102,241,.06) 0%,transparent 70%);background-attachment:fixed;color:#dde2ed;min-height:100vh;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}}
.topbar{{background:rgba(7,9,18,.76);backdrop-filter:blur(24px) saturate(160%);-webkit-backdrop-filter:blur(24px) saturate(160%);padding:0 24px;height:54px;display:flex;align-items:center;gap:16px;border-bottom:1px solid rgba(255,255,255,.08);position:sticky;top:0;z-index:50;box-shadow:0 1px 0 rgba(255,255,255,.04),0 2px 12px rgba(0,0,0,.28)}}
.topbar a{{color:#8b95b0;text-decoration:none;font-size:.84rem;display:flex;align-items:center;gap:5px;transition:color .15s;padding:6px 10px;border-radius:7px;min-height:36px}}
.topbar a:hover{{color:#dde2ed;background:rgba(255,255,255,.07)}}
.topbar a:focus-visible{{outline:none;box-shadow:0 0 0 2px rgba(7,9,18,1),0 0 0 4px #6366f1}}
.topbar-title{{color:#dde2ed;font-weight:600;font-size:.9rem}}
.content{{max-width:720px;margin:32px auto;padding:0 16px 48px}}
.card{{background:rgba(255,255,255,.04);backdrop-filter:blur(22px) saturate(160%) brightness(1.03);-webkit-backdrop-filter:blur(22px) saturate(160%) brightness(1.03);border-radius:12px;border:1px solid rgba(255,255,255,.085);box-shadow:0 2px 8px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.055);padding:22px;margin-bottom:16px;position:relative;overflow:hidden}}
.card::before{{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent 5%,rgba(255,255,255,.11) 40%,rgba(255,255,255,.13) 50%,rgba(255,255,255,.11) 60%,transparent 95%);pointer-events:none}}
.card::after{{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:linear-gradient(135deg,rgba(255,255,255,.05) 0%,transparent 45%);pointer-events:none;border-radius:inherit}}
.meta{{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid rgba(255,255,255,.08)}}
.meta-item{{font-size:.83rem;color:#8b95b0}}
.meta-item b{{color:#dde2ed}}
</style>
</head>
<body>
<div class="topbar">
  <a href="javascript:history.back()" aria-label="返回上一页">← 返回</a>
  <span class="topbar-title">📋 报告 #{report_id}</span>
</div>
<div class="content">
  <div class="card">
    <div class="meta">
      <div class="meta-item"><b>报告 ID</b>：#{report_id}</div>
      <div class="meta-item"><b>用户</b>：@{username}</div>
      <div class="meta-item"><b>提交时间</b>：{created_at}</div>
      <div class="meta-item">{status_badge}</div>
    </div>
    {rows_html if rows_html else "<p style='color:#5a6480;font-style:italic'>（无字段内容）</p>"}
  </div>
</div>
</body>
</html>"""


_ADMIN_CSS = """
:root{
--pri:#6366f1;--pri-d:#4f46e5;--pri-l:rgba(99,102,241,.15);
--suc:#10b981;--suc-l:rgba(16,185,129,.12);
--dan:#f43f5e;--dan-l:rgba(244,63,94,.12);--warn:#f59e0b;
--txt:#dde2ed;--txt2:#8b95b0;--txt3:#5a6480;
--bdr:rgba(255,255,255,.08);--bdr2:rgba(255,255,255,.13);
--bg:#070912;--card:rgba(255,255,255,.04);--hov:rgba(255,255,255,.065);
--input-bg:rgba(0,0,0,.3);
--sh:0 2px 8px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.055);
--sh2:0 8px 32px rgba(0,0,0,.6),0 0 0 1px rgba(255,255,255,.06);
--r:10px;--r2:14px;--r3:18px;
--glass-blur:22px;--glass-sat:160%;--glass-bright:1.03;
--refract-top:linear-gradient(90deg,transparent 5%,rgba(255,255,255,.11) 40%,rgba(255,255,255,.13) 50%,rgba(255,255,255,.11) 60%,transparent 95%);
--refract-shine:linear-gradient(135deg,rgba(255,255,255,.05) 0%,transparent 45%);
--focus-ring:0 0 0 2px var(--bg),0 0 0 4px var(--pri);
--tab-h:62px;--safe-b:env(safe-area-inset-bottom,0px);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","SF Pro Display",Roboto,"Helvetica Neue",Arial,sans-serif;background:var(--bg);background-image:radial-gradient(ellipse 120% 60% at 50% -5%,rgba(99,102,241,.08) 0%,transparent 60%),radial-gradient(ellipse 80% 60% at 5% 85%,rgba(16,185,129,.04) 0%,transparent 55%),radial-gradient(ellipse 70% 50% at 95% 50%,rgba(99,102,241,.03) 0%,transparent 60%);background-attachment:fixed;color:var(--txt);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;overflow-x:hidden}
/* ── Skip nav ────────────────────────────────────────────────────────── */
.skip-nav{position:absolute;top:-100%;left:16px;background:var(--pri);color:#fff;padding:8px 16px;border-radius:var(--r);font-size:.84rem;font-weight:600;text-decoration:none;z-index:9999;transition:top .1s}
.skip-nav:focus{top:8px}
/* ── Layout ──────────────────────────────────────────────────────────── */
.layout{display:flex;flex-direction:column;min-height:100%;min-height:100dvh}
.main{flex:1;display:flex;flex-direction:column;padding-bottom:calc(var(--tab-h) + var(--safe-b))}
/* ── Top bar ─────────────────────────────────────────────────────────── */
.topbar{background:rgba(7,9,18,.76);backdrop-filter:blur(24px) saturate(170%) brightness(var(--glass-bright));-webkit-backdrop-filter:blur(24px) saturate(170%) brightness(var(--glass-bright));border-bottom:1px solid var(--bdr);padding:0 16px;height:54px;display:flex;align-items:center;justify-content:space-between;gap:12px;position:sticky;top:0;z-index:50;box-shadow:0 1px 0 rgba(255,255,255,.045),0 2px 14px rgba(0,0,0,.28)}
.topbar::after{content:'';position:absolute;bottom:-1px;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent 0%,rgba(255,255,255,.07) 30%,rgba(255,255,255,.07) 70%,transparent 100%);pointer-events:none}
.topbar-left{display:flex;align-items:center;gap:8px;min-width:0;flex:1}
.topbar-icon{font-size:1.05rem;line-height:1;flex-shrink:0}
.topbar-title{font-size:.92rem;font-weight:600;color:#dde2ed;letter-spacing:-.012em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.topbar-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
.topbar-stat{font-size:.73rem;color:var(--txt3);background:rgba(255,255,255,.048);border:1px solid var(--bdr);padding:3px 10px;border-radius:20px;white-space:nowrap}
.topbar-logout{display:flex;align-items:center;justify-content:center;padding:6px 10px;min-height:34px;border-radius:var(--r);color:var(--txt3);text-decoration:none;font-size:.75rem;border:1px solid var(--bdr);background:rgba(255,255,255,.04);transition:color .14s,background .14s;white-space:nowrap}
.topbar-logout:hover{color:var(--txt);background:rgba(255,255,255,.09)}
.topbar-logout:focus-visible{outline:none;box-shadow:var(--focus-ring)}
/* ── Sub navigation (horizontal scrollable pills) ────────────────────── */
.sub-nav{display:none;gap:7px;padding:10px 16px 11px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;border-bottom:1px solid var(--bdr);background:rgba(5,7,16,.72);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);position:sticky;top:54px;z-index:40}
.sub-nav.visible{display:flex}
.sub-nav::-webkit-scrollbar{display:none}
.sub-btn{flex-shrink:0;display:inline-flex;align-items:center;gap:4px;padding:6px 13px;min-height:30px;border-radius:20px;border:1px solid var(--bdr);background:rgba(255,255,255,.05);color:var(--txt2);font-size:.76rem;font-weight:500;font-family:inherit;cursor:pointer;transition:background .14s,color .14s,border-color .14s;white-space:nowrap;-webkit-tap-highlight-color:transparent}
.sub-btn:hover{background:rgba(255,255,255,.1);color:var(--txt)}
.sub-btn.active{background:rgba(99,102,241,.18);color:#a5b4fc;border-color:rgba(99,102,241,.32)}
.sub-btn:focus-visible{outline:none;box-shadow:0 0 0 2px var(--pri)}
/* ── Content ─────────────────────────────────────────────────────────── */
.content{flex:1;padding:16px 16px 20px}
/* ── Stats overview ──────────────────────────────────────────────────── */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:18px}
.stat-card{background:var(--card);backdrop-filter:blur(var(--glass-blur)) saturate(var(--glass-sat));-webkit-backdrop-filter:blur(var(--glass-blur)) saturate(var(--glass-sat));border-radius:var(--r2);border:1px solid var(--bdr);padding:13px 6px;text-align:center;position:relative;overflow:hidden}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:var(--refract-top);pointer-events:none}
.stat-card::after{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:var(--refract-shine);pointer-events:none;border-radius:inherit}
.stat-card.c-warn{border-color:rgba(245,158,11,.22);background:rgba(245,158,11,.06)}
.stat-card.c-suc{border-color:rgba(16,185,129,.22);background:rgba(16,185,129,.06)}
.stat-card.c-dan{border-color:rgba(244,63,94,.22);background:rgba(244,63,94,.06)}
.stat-num{font-size:1.3rem;font-weight:700;color:var(--txt);line-height:1;margin-bottom:4px;position:relative;z-index:1}
.stat-card.c-warn .stat-num{color:#fde68a}
.stat-card.c-suc .stat-num{color:#6ee7b7}
.stat-card.c-dan .stat-num{color:#fca5a5}
.stat-lbl{font-size:.63rem;color:var(--txt3);font-weight:500;letter-spacing:.01em;position:relative;z-index:1}
/* ── Alerts ──────────────────────────────────────────────────────────── */
.alert{display:flex;align-items:center;gap:10px;padding:11px 16px;border-radius:var(--r2);margin-bottom:16px;font-size:.83rem;font-weight:500}
.alert-success{background:rgba(16,185,129,.1);color:#6ee7b7;border:1px solid rgba(16,185,129,.22)}
/* ── Tab panes ───────────────────────────────────────────────────────── */
.tab-pane{display:none}
.tab-pane.active{display:block;animation:fadeIn .18s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
/* ── Section title ───────────────────────────────────────────────────── */
.section-title{font-size:.67rem;font-weight:700;color:var(--txt3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:14px;padding-bottom:9px;border-bottom:1px solid var(--bdr)}
/* ── Card (Liquid Glass) ─────────────────────────────────────────────── */
.card{background:var(--card);backdrop-filter:blur(var(--glass-blur)) saturate(var(--glass-sat)) brightness(var(--glass-bright));-webkit-backdrop-filter:blur(var(--glass-blur)) saturate(var(--glass-sat)) brightness(var(--glass-bright));border-radius:var(--r2);border:1px solid var(--bdr);box-shadow:0 2px 8px rgba(0,0,0,.38),inset 0 1px 0 rgba(255,255,255,.055);padding:16px;margin-bottom:14px;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:var(--refract-top);pointer-events:none;z-index:1}
.card::after{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:var(--refract-shine);pointer-events:none;border-radius:inherit;z-index:0}
/* ── Form fields ─────────────────────────────────────────────────────── */
.field{margin-bottom:14px}
.field-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
label{display:block;font-size:.7rem;font-weight:600;color:var(--txt2);margin-bottom:5px;text-transform:uppercase;letter-spacing:.06em}
.hint{font-size:.73rem;color:var(--txt3);margin-top:5px;line-height:1.5}
.hint code{background:rgba(255,255,255,.08);border-radius:3px;padding:1px 5px;font-size:.78em;color:#a5b4fc}
input[type=text],input[type=number],input[type=url],textarea,select{width:100%;padding:9px 12px;border:1px solid rgba(255,255,255,.1);border-radius:var(--r);font-size:.84rem;font-family:inherit;background:var(--input-bg);color:var(--txt);transition:border-color .15s,box-shadow .15s;-webkit-appearance:none;min-height:42px}
input[type=text]::placeholder,input[type=number]::placeholder,input[type=url]::placeholder,textarea::placeholder{color:var(--txt3);opacity:.6}
input[type=text]:focus,input[type=number]:focus,input[type=url]:focus,textarea:focus,select:focus{outline:none;border-color:rgba(99,102,241,.55);box-shadow:0 0 0 3px rgba(99,102,241,.13)}
input[type=text][readonly]{background:rgba(0,0,0,.15);color:var(--txt3);cursor:default;border-color:rgba(255,255,255,.06)}
input[type=checkbox]{accent-color:var(--pri);width:16px;height:16px;cursor:pointer}
textarea{resize:vertical;min-height:80px}
select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%238b95b0' d='M6 8L0 0h12z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 11px center;padding-right:32px;cursor:pointer}
select option{background:#111827;color:var(--txt)}
/* ── Buttons ─────────────────────────────────────────────────────────── */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:9px 16px;min-height:42px;border:none;border-radius:var(--r);cursor:pointer;font-size:.84rem;font-weight:500;font-family:inherit;transition:background .14s,box-shadow .14s,transform .12s;text-decoration:none;white-space:nowrap;line-height:1.4;-webkit-tap-highlight-color:transparent}
.btn:focus-visible{outline:none;box-shadow:var(--focus-ring)}
.btn:active{transform:scale(.97)}
.btn-primary{background:var(--pri);color:#fff;box-shadow:0 1px 5px rgba(99,102,241,.38)}
.btn-primary:hover{background:var(--pri-d);box-shadow:0 4px 12px rgba(99,102,241,.42)}
.btn-danger{background:var(--dan);color:#fff;box-shadow:0 1px 4px rgba(244,63,94,.32)}
.btn-danger:hover{background:#e11d48}
.btn-success{background:var(--suc);color:#fff;box-shadow:0 1px 4px rgba(16,185,129,.32)}
.btn-success:hover{background:#059669}
.btn-secondary{background:rgba(255,255,255,.07);color:var(--txt2);border:1px solid var(--bdr2)}
.btn-secondary:hover{background:rgba(255,255,255,.12);color:var(--txt)}
.btn-sm{padding:5px 11px;font-size:.76rem;min-height:32px}
.btn-add{display:flex;align-items:center;justify-content:center;gap:6px;background:rgba(99,102,241,.07);color:#a5b4fc;border:1.5px dashed rgba(99,102,241,.3);padding:10px 16px;min-height:44px;width:100%;border-radius:var(--r);cursor:pointer;font-size:.81rem;font-weight:500;margin-top:8px;transition:background .15s,border-color .15s,color .15s;font-family:inherit;-webkit-tap-highlight-color:transparent}
.btn-add:hover{background:rgba(99,102,241,.14);border-color:rgba(99,102,241,.52);color:#c7d2fe}
.btn-add:focus-visible{outline:none;box-shadow:var(--focus-ring)}
/* ── Editor rows ─────────────────────────────────────────────────────── */
.editor-row{display:flex;gap:8px;align-items:center;margin-bottom:6px;padding:9px 11px;background:rgba(0,0,0,.22);border:1px solid var(--bdr);border-radius:var(--r);transition:border-color .15s}
.editor-row:hover{border-color:rgba(255,255,255,.13)}
.editor-row:focus-within{border-color:rgba(99,102,241,.35)}
.editor-row input,.editor-row select{flex:1;min-width:60px}
.tpl-field-card{background:rgba(0,0,0,.18);border:1px solid var(--bdr);border-radius:var(--r);margin-bottom:8px;overflow:hidden;transition:border-color .15s}
.tpl-field-card:hover{border-color:rgba(255,255,255,.13)}
.tpl-field-card:focus-within{border-color:rgba(99,102,241,.35)}
.tpl-field-card .editor-row{background:transparent;border:none;border-radius:0;margin-bottom:0}
/* ── Save bar ────────────────────────────────────────────────────────── */
.save-bar{background:rgba(7,9,18,.9);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-top:1px solid var(--bdr);padding:12px 16px;display:flex;justify-content:flex-end;align-items:center;gap:10px;position:sticky;bottom:calc(var(--tab-h) + var(--safe-b));box-shadow:0 -6px 24px rgba(0,0,0,.35);margin-top:8px}
/* ── Table ───────────────────────────────────────────────────────────── */
.table{width:100%;border-collapse:collapse;font-size:.82rem}
.table th,.table td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--bdr)}
.table th{background:rgba(0,0,0,.22);font-weight:600;color:var(--txt3);font-size:.66rem;text-transform:uppercase;letter-spacing:.09em;white-space:nowrap}
.table tbody tr{transition:background .1s}
.table tbody tr:hover td{background:rgba(255,255,255,.028)}
.table tbody tr:focus-within td{background:rgba(99,102,241,.04)}
.table td input{padding:5px 8px;border:1px solid var(--bdr2);border-radius:6px;font-size:.8rem;width:120px;background:var(--input-bg);color:var(--txt)}
.muted{color:var(--txt3);font-style:italic;padding:16px 0;display:block}
.badge{display:inline-flex;align-items:center;justify-content:center;background:#f43f5e;color:#fff;border-radius:9px;font-size:.62rem;font-weight:700;min-width:17px;height:17px;padding:0 5px;margin-left:4px;vertical-align:middle;box-shadow:0 1px 4px rgba(244,63,94,.4)}
/* ── Rich Text Editor ────────────────────────────────────────────────── */
.rte-wrap{border:1px solid rgba(255,255,255,.1);border-radius:var(--r);overflow:hidden;background:var(--input-bg)}
.rte-wrap:focus-within{border-color:rgba(99,102,241,.55);box-shadow:0 0 0 3px rgba(99,102,241,.13)}
.rte-toolbar{display:flex;flex-wrap:wrap;gap:2px;padding:6px 8px;background:rgba(0,0,0,.22);border-bottom:1px solid var(--bdr)}
.rte-btn{padding:5px 8px;min-width:32px;min-height:32px;border:1px solid transparent;border-radius:6px;background:none;cursor:pointer;font-size:.81rem;font-family:inherit;color:var(--txt2);transition:background .1s,color .1s;line-height:1.4;-webkit-tap-highlight-color:transparent}
.rte-btn:hover{background:rgba(255,255,255,.1);border-color:var(--bdr2);color:var(--txt)}
.rte-btn:focus-visible{outline:none;box-shadow:inset 0 0 0 2px var(--pri)}
.rte-body{padding:10px 12px;min-height:80px;outline:none;font-size:.84rem;line-height:1.6;font-family:inherit;word-break:break-word;color:var(--txt)}
.rte-body:empty:before{content:attr(data-ph);color:var(--txt3);pointer-events:none;display:block}
.rte-pills{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}
.rte-pill{padding:3px 10px;background:rgba(99,102,241,.1);color:#a5b4fc;border:1px solid rgba(99,102,241,.22);border-radius:12px;cursor:pointer;font-size:.72rem;font-weight:500;transition:background .15s,color .15s;font-family:inherit;min-height:28px}
.rte-pill:hover{background:rgba(99,102,241,.2);border-color:rgba(99,102,241,.48);color:#c7d2fe}
.rte-pill:focus-visible{outline:none;box-shadow:var(--focus-ring)}
/* ── Bottom Tab Bar ──────────────────────────────────────────────────── */
.tab-bar{position:fixed;bottom:0;left:0;right:0;height:calc(var(--tab-h) + var(--safe-b));padding-bottom:var(--safe-b);background:rgba(5,7,16,.9);backdrop-filter:blur(24px) saturate(160%);-webkit-backdrop-filter:blur(24px) saturate(160%);border-top:1px solid rgba(255,255,255,.09);display:flex;align-items:stretch;z-index:100;box-shadow:0 -1px 0 rgba(255,255,255,.05),0 -4px 24px rgba(0,0,0,.35)}
.tab-bar::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent 0%,rgba(255,255,255,.1) 25%,rgba(255,255,255,.1) 75%,transparent 100%);pointer-events:none}
.tab-btn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;padding:8px 4px 6px;background:none;border:none;cursor:pointer;color:var(--txt3);font-family:inherit;transition:color .15s;position:relative;min-width:0;-webkit-tap-highlight-color:transparent;user-select:none}
.tab-btn:active{opacity:.7}
.tab-icon{font-size:1.25rem;line-height:1;transition:transform .22s cubic-bezier(.34,1.56,.64,1)}
.tab-label{font-size:.6rem;font-weight:500;letter-spacing:.015em;white-space:nowrap;margin-top:1px}
.tab-badge{position:absolute;top:5px;right:calc(50% - 22px);background:#f43f5e;color:#fff;border-radius:9px;font-size:.55rem;font-weight:700;min-width:16px;height:16px;padding:0 4px;display:flex;align-items:center;justify-content:center;box-shadow:0 1px 4px rgba(244,63,94,.55);border:1.5px solid rgba(5,7,16,.9)}
.tab-btn.active{color:var(--pri)}
.tab-btn.active .tab-icon{transform:translateY(-2px) scale(1.12)}
.tab-btn:focus-visible{outline:none;background:rgba(255,255,255,.05)}
/* ── Responsive ──────────────────────────────────────────────────────── */
@media(min-width:540px){.field-row{grid-template-columns:1fr 1fr}.stats-row{grid-template-columns:repeat(4,1fr)}}
@media(max-width:539px){.field-row{grid-template-columns:1fr}}
"""

_ADMIN_JS = """
(function(){
  // ── Telegram WebApp theme integration ─────────────────────────────────────
  (function applyTGTheme(){
    var tg=window.Telegram&&window.Telegram.WebApp;
    if(!tg)return;
    var p=tg.themeParams||{};
    var r=document.documentElement;
    function sv(v,val){if(val)r.style.setProperty(v,val);}
    if(p.bg_color){sv('--bg',p.bg_color);}
    if(p.secondary_bg_color){sv('--sb-bg',p.secondary_bg_color+'ee');}
    if(p.text_color){sv('--txt',p.text_color);}
    if(p.hint_color){sv('--txt2',p.hint_color);}
    if(p.link_color){sv('--pri',p.link_color);}
    if(p.button_color){sv('--pri',p.button_color);}
    if(p.accent_text_color){sv('--pri',p.accent_text_color);}
    if(p.destructive_text_color){sv('--dan',p.destructive_text_color);}
    if(p.section_bg_color&&/^#[0-9a-fA-F]{3,8}$/.test(p.section_bg_color)){sv('--card','color-mix(in srgb,'+p.section_bg_color+' 65%,transparent)');}
    if(p.header_bg_color){sv('--bg',p.header_bg_color);}
    try{if(tg.expand)tg.expand();}catch(e){}
    try{if(tg.disableVerticalSwipes)tg.disableVerticalSwipes();}catch(e){}
    if(tg.onEvent)tg.onEvent('themeChanged',applyTGTheme);
  })();

  // ── Section / Tab routing ─────────────────────────────────────────────────
  var sectionMeta={
    home:{icon:'⏳',label:'待审核报告',tabs:['pending'],subNavId:null},
    users:{icon:'👥',label:'用户管理',tabs:['blacklist','broadcast'],subNavId:'sub-nav-users'},
    stats:{icon:'📊',label:'报告统计',tabs:['reports'],subNavId:null},
    settings:{icon:'⚙️',label:'系统设置',tabs:['basic','welcome','keyboard','template','texts','review','child-bots'],subNavId:'sub-nav-settings'}
  };
  var tabSection={};
  Object.keys(sectionMeta).forEach(function(sec){
    sectionMeta[sec].tabs.forEach(function(t){tabSection[t]=sec;});
  });
  var currentSection='home';
  var sectionLastTab={home:'pending',users:'blacklist',stats:'reports',settings:'basic'};

  var tabBtns=document.querySelectorAll('.tab-btn');
  var subBtns=document.querySelectorAll('.sub-btn');
  var tabPanes=document.querySelectorAll('.tab-pane');
  var saveBar=document.getElementById('settings-save-bar');
  var topbarIcon=document.getElementById('topbar-icon');
  var topbarTitle=document.getElementById('topbar-title');
  var subNavEls={};
  ['sub-nav-users','sub-nav-settings'].forEach(function(id){
    var el=document.getElementById(id);
    if(el)subNavEls[id]=el;
  });
  var noSaveTabs=['pending','blacklist','broadcast','reports','child-bots'];

  function switchTab(tab){
    tabPanes.forEach(function(p){p.classList.remove('active');});
    var pane=document.getElementById('pane-'+tab);
    if(pane)pane.classList.add('active');
    subBtns.forEach(function(b){b.classList.toggle('active',b.dataset.tab===tab);});
    if(saveBar)saveBar.style.display=noSaveTabs.indexOf(tab)>=0?'none':'flex';
    var sec=tabSection[tab]||currentSection;
    sectionLastTab[sec]=tab;
    if(tab==='review'&&_rteMap['push_template'])_rteMap['push_template'].refreshPills();
    if(tab==='broadcast'&&_rteMap['broadcast_text'])_rteMap['broadcast_text'].refreshPills();
  }

  function switchSection(section){
    currentSection=section;
    tabBtns.forEach(function(b){
      b.classList.toggle('active',b.dataset.section===section);
      b.setAttribute('aria-selected',b.dataset.section===section?'true':'false');
    });
    // Show/hide sub-navs
    Object.keys(subNavEls).forEach(function(id){
      var el=subNavEls[id];
      var meta=sectionMeta[section];
      var show=meta&&meta.subNavId===id;
      el.classList.toggle('visible',show);
    });
    // Update topbar
    var meta=sectionMeta[section]||{};
    if(topbarIcon)topbarIcon.textContent=meta.icon||'';
    if(topbarTitle)topbarTitle.textContent=meta.label||section;
    // Switch to last tab in section
    var tabs=meta.tabs||[];
    var lastTab=sectionLastTab[section]||tabs[0]||'pending';
    switchTab(lastTab);
  }

  tabBtns.forEach(function(btn){
    btn.addEventListener('click',function(){switchSection(btn.dataset.section);});
  });
  subBtns.forEach(function(btn){
    btn.addEventListener('click',function(){switchTab(btn.dataset.tab);});
  });

  // Restore from URL param/hash
  (function(){
    var params=new URLSearchParams(window.location.search);
    var tab=params.get('tab')||'';
    if(!tab){var h=window.location.hash.replace(/^#(tab=)?/,'');if(h)tab=h;}
    if(tab&&tabSection[tab]){
      switchSection(tabSection[tab]);
      switchTab(tab);
    } else {
      switchSection('home');
    }
  })();

  // Start Buttons Editor
  var startBtnsData=__START_BUTTONS__;
  var startRows=document.getElementById('start-btn-rows');
  function makeStartRow(item){
    var row=document.createElement('div'); row.className='editor-row';
    var textIn=document.createElement('input');
    textIn.type='text'; textIn.placeholder='按钮文字'; textIn.value=item.text||'';
    textIn.dataset.field='text'; textIn.style.flex='1';
    var urlIn=document.createElement('input');
    urlIn.type='text'; urlIn.placeholder='链接 URL（https://...）'; urlIn.value=item.url||'';
    urlIn.dataset.field='url'; urlIn.style.flex='2';
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){row.remove();});
    row.appendChild(textIn); row.appendChild(urlIn); row.appendChild(rm);
    return row;
  }
  startBtnsData.forEach(function(item){startRows.appendChild(makeStartRow(item));});
  document.getElementById('start-btn-add').addEventListener('click',function(){
    startRows.appendChild(makeStartRow({text:'',url:''}));
  });
  function serializeStartBtns(){
    var result=[];
    startRows.querySelectorAll('.editor-row').forEach(function(row){
      var text=row.querySelector('[data-field=text]').value.trim();
      var url=row.querySelector('[data-field=url]').value.trim();
      if(text&&url) result.push({text:text,url:url});
    });
    document.getElementById('start_buttons_json').value=JSON.stringify(result);
  }

  // Keyboard Buttons Editor
  var kbData=__KB_BUTTONS__;
  var kbRows=document.getElementById('kb-rows');
  var KB_ACTIONS=[
    {value:'write_report',label:'写报告（内置）'},
    {value:'search_help',label:'查阅报告（内置）'},
    {value:'my_reports',label:'我的报告（内置）'},
    {value:'contact',label:'联系管理员（内置）'},
    {value:'usage',label:'操作方式（内置）'},
    {value:'text',label:'自定义回复文本'}
  ];
  function makeKbRow(item){
    var row=document.createElement('div'); row.className='editor-row';
    var textIn=document.createElement('input');
    textIn.type='text'; textIn.placeholder='按钮文字'; textIn.value=item.text||'';
    textIn.dataset.field='text';
    var sel=document.createElement('select');
    sel.dataset.field='action'; sel.style.flex='none'; sel.style.width='180px';
    KB_ACTIONS.forEach(function(a){
      var opt=document.createElement('option');
      opt.value=a.value; opt.textContent=a.label;
      if(item.action===a.value) opt.selected=true;
      sel.appendChild(opt);
    });
    var valIn=document.createElement('input');
    valIn.type='text'; valIn.placeholder='回复内容'; valIn.value=item.value||'';
    valIn.dataset.field='value';
    valIn.style.display=(item.action==='text')?'':'none';
    sel.addEventListener('change',function(){
      valIn.style.display=sel.value==='text'?'':'none';
    });
    var rowIn=document.createElement('input');
    rowIn.type='text'; rowIn.placeholder='行号'; rowIn.value=item.row||'';
    rowIn.dataset.field='row'; rowIn.style.width='50px'; rowIn.style.flex='none';
    rowIn.title='相同行号的按钮同行显示，留空则独占一行';
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){row.remove();});
    row.appendChild(textIn); row.appendChild(sel); row.appendChild(valIn); row.appendChild(rowIn); row.appendChild(rm);
    return row;
  }
  kbData.forEach(function(item){kbRows.appendChild(makeKbRow(item));});
  document.getElementById('kb-add').addEventListener('click',function(){
    kbRows.appendChild(makeKbRow({text:'',action:'write_report',value:''}));
  });
  function serializeKb(){
    var result=[];
    kbRows.querySelectorAll('.editor-row').forEach(function(row){
      var text=row.querySelector('[data-field=text]').value.trim();
      var action=row.querySelector('[data-field=action]').value;
      var value=row.querySelector('[data-field=value]').value.trim();
      var rowNum=row.querySelector('[data-field=row]').value.trim();
      if(text){
        var item={text:text,action:action};
        if(action==='text'&&value) item.value=value;
        if(rowNum) item.row=rowNum;
        result.push(item);
      }
    });
    document.getElementById('keyboard_buttons_json').value=JSON.stringify(result);
  }

  // Report Template Editor
  var tplData=__TEMPLATE__;
  var tplFieldsEl=document.getElementById('template-fields');
  var tplNameIn=document.getElementById('template-name');
  tplNameIn.value=tplData.name||'';
  function makeTplRow(field){
    var card=document.createElement('div'); card.className='tpl-field-card';
    // Row 1: key, label, type, required, remove
    var row1=document.createElement('div'); row1.className='editor-row'; row1.style.marginBottom='4px';
    var keyIn=document.createElement('input');
    keyIn.type='text'; keyIn.placeholder='英文标识（如 title）'; keyIn.value=field.key||'';
    keyIn.dataset.field='key'; keyIn.style.flex='1';
    var labelIn=document.createElement('input');
    labelIn.type='text'; labelIn.placeholder='显示名称（如 标题）'; labelIn.value=field.label||'';
    labelIn.dataset.field='label'; labelIn.style.flex='1';
    var typeSel=document.createElement('select');
    typeSel.dataset.field='type'; typeSel.style.flex='none'; typeSel.style.width='80px';
    [{value:'text',label:'文本'},{value:'photo',label:'图片'}].forEach(function(o){
      var opt=document.createElement('option');
      opt.value=o.value; opt.textContent=o.label;
      if((field.type||'text')===o.value) opt.selected=true;
      typeSel.appendChild(opt);
    });
    var reqLabel=document.createElement('label');
    reqLabel.style.cssText='display:flex;align-items:center;gap:4px;font-weight:normal;font-size:.85rem;white-space:nowrap;flex:none;text-transform:none;letter-spacing:0;color:#8b95b0;';
    var reqCheck=document.createElement('input');
    reqCheck.type='checkbox'; reqCheck.dataset.field='required'; reqCheck.style.margin='0';
    reqCheck.checked=(field.required!==false);
    reqLabel.appendChild(reqCheck); reqLabel.appendChild(document.createTextNode('必填'));
    var rm=document.createElement('button');
    rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
    rm.addEventListener('click',function(){card.remove();});
    row1.appendChild(keyIn); row1.appendChild(labelIn); row1.appendChild(typeSel); row1.appendChild(reqLabel); row1.appendChild(rm);
    // Row 2: hint textarea
    var row2=document.createElement('div'); row2.style.cssText='padding:0 12px 10px;';
    var hintIn=document.createElement('textarea');
    hintIn.placeholder='字段说明（选填）：例如"请填写今日工作摘要"，显示给用户作为填写提示，支持多行';
    hintIn.value=field.hint||''; hintIn.dataset.field='hint'; hintIn.style.cssText='width:100%;rows:2;resize:vertical;min-height:52px;';
    hintIn.rows=2;
    row2.appendChild(hintIn);
    card.appendChild(row1); card.appendChild(row2);
    return card;
  }
  (tplData.fields||[]).forEach(function(f){tplFieldsEl.appendChild(makeTplRow(f));});
  document.getElementById('template-add').addEventListener('click',function(){
    tplFieldsEl.appendChild(makeTplRow({key:'',label:'',hint:'',required:true,type:'text'}));
  });
  function serializeTemplate(){
    var fields=[];
    tplFieldsEl.querySelectorAll('.tpl-field-card').forEach(function(card){
      var key=card.querySelector('[data-field=key]').value.trim();
      var label=card.querySelector('[data-field=label]').value.trim();
      var hint=card.querySelector('[data-field=hint]').value.trim();
      var type=card.querySelector('[data-field=type]').value;
      var required=card.querySelector('[data-field=required]').checked;
      if(key&&label) fields.push({key:key,label:label,hint:hint,required:required,type:type});
    });
    var tpl={name:tplNameIn.value.trim()||'模板',fields:fields};
    document.getElementById('report_template_json').value=JSON.stringify(tpl);
  }

  document.getElementById('settings-form').addEventListener('submit',function(){
    Object.keys(_rteMap).forEach(function(k){if(_rteMap[k])_rteMap[k].sync();});
    serializeStartBtns();
    serializeKb();
    serializeTemplate();
  });

  function getTplTextFields(){
    var fields=[];
    tplFieldsEl.querySelectorAll('.tpl-field-card').forEach(function(card){
      var key=card.querySelector('[data-field=key]').value.trim();
      var label=card.querySelector('[data-field=label]').value.trim();
      var type=card.querySelector('[data-field=type]').value;
      if(key&&label&&type!=='photo') fields.push({key:key,label:label});
    });
    return fields;
  }

  // Rich Text Editor
  function serializeRTENode(node){
    var out='';
    node.childNodes.forEach(function(n){
      if(n.nodeType===3){
        out+=n.textContent.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      } else if(n.nodeType===1){
        var t=n.tagName.toLowerCase();
        var inner=serializeRTENode(n);
        if(t==='b'||t==='strong') out+='<b>'+inner+'</b>';
        else if(t==='i'||t==='em') out+='<i>'+inner+'</i>';
        else if(t==='u') out+='<u>'+inner+'</u>';
        else if(t==='s'||t==='strike'||t==='del') out+='<s>'+inner+'</s>';
        else if(t==='code') out+='<code>'+inner+'</code>';
        else if(t==='a'){var href=(n.getAttribute('href')||'').replace(/"/g,'&quot;');out+='<a href="'+href+'">'+inner+'</a>';}
        else if(t==='br') out+='\\n';
        else if(t==='div'||t==='p') out+=(inner||'')+'\\n';
        else out+=inner;
      }
    });
    return out;
  }
  var _rteMap={};
  function RichTextEditor(ta,getPills){
    var self=this; self._ta=ta; self._getPills=getPills||null; self._pd=null;
    var wrap=document.createElement('div'); wrap.className='rte-wrap';
    ta.parentNode.insertBefore(wrap,ta); ta.style.display='none';
    if(getPills){var pd=document.createElement('div');pd.className='rte-pills';wrap.appendChild(pd);self._pd=pd;}
    var tb=document.createElement('div'); tb.className='rte-toolbar'; wrap.appendChild(tb);
    var body=document.createElement('div'); body.className='rte-body'; body.contentEditable='true';
    body.setAttribute('data-ph',ta.getAttribute('placeholder')||'输入内容…');
    var existing=ta.value; if(existing) body.innerHTML=existing.replace(/\\n/g,'<br>');
    wrap.appendChild(body); self._body=body;
    var tools=[
      {cmd:'bold',html:'<b>B</b>',title:'粗体'},
      {cmd:'italic',html:'<i>I</i>',title:'斜体'},
      {cmd:'underline',html:'<u>U</u>',title:'下划线'},
      {cmd:'strikeThrough',html:'<s>S</s>',title:'删除线'},
      {cmd:'code',html:'<code style="font-size:.8rem">&lt;/&gt;</code>',title:'代码'},
      {cmd:'link',html:'🔗',title:'添加链接'},
      {cmd:'unlink',html:'🔗✕',title:'移除链接'},
      {cmd:'undo',html:'↩',title:'撤销'},
      {cmd:'redo',html:'↪',title:'重做'}
    ];
    tools.forEach(function(t){
      var btn=document.createElement('button'); btn.type='button';
      btn.innerHTML=t.html; btn.title=t.title; btn.className='rte-btn';
      btn.addEventListener('mousedown',function(e){
        e.preventDefault(); body.focus();
        if(t.cmd==='code'){
          var sel=window.getSelection();
          if(sel&&sel.rangeCount>0&&!sel.isCollapsed){
            var range=sel.getRangeAt(0);
            var codeEl=document.createElement('code');
            try{range.surroundContents(codeEl);}catch(ex){var et=range.toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');document.execCommand('insertHTML',false,'<code>'+et+'</code>');}
          } else {document.execCommand('insertHTML',false,'<code></code>');}
        } else if(t.cmd==='link'){
          var sel=window.getSelection(); var st=sel?sel.toString():'';
          var url=prompt('输入链接地址（https://...）','');
          if(url){
            if(st){document.execCommand('createLink',false,url);}
            else{var su=url.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');document.execCommand('insertHTML',false,'<a href="'+su+'">'+su+'</a>');}
          }
        } else if(t.cmd==='unlink'){document.execCommand('unlink');}
        else if(t.cmd==='undo'){document.execCommand('undo');}
        else if(t.cmd==='redo'){document.execCommand('redo');}
        else{document.execCommand(t.cmd);}
      });
      tb.appendChild(btn);
    });
    self.sync=function(){var raw=serializeRTENode(body);self._ta.value=raw.replace(/\\n+$/,'');};
    self.refreshPills=function(){
      if(!self._pd||!self._getPills)return;
      var pills=self._getPills(); self._pd.innerHTML='';
      pills.forEach(function(p){
        var btn=document.createElement('button'); btn.type='button'; btn.className='rte-pill';
        btn.textContent=p.label; btn.title='插入: '+p.insert;
        btn.addEventListener('click',function(){body.focus();document.execCommand('insertText',false,p.insert);});
        self._pd.appendChild(btn);
      });
    };
  }
  function getPushTemplatePills(){
    var pills=[{label:'报告ID',insert:'{id}'},{label:'用户名',insert:'{username}'},{label:'报告链接',insert:'{link}'}];
    getTplTextFields().forEach(function(f){pills.push({label:f.label,insert:'{'+f.key+'}'});});
    return pills;
  }
  function getBroadcastPills(){
    var pills=[];
    getTplTextFields().forEach(function(f){pills.push({label:f.label,insert:'{'+f.key+'}'});});
    return pills;
  }
  ['start_text','search_help_text','contact_text','usage_text'].forEach(function(name){
    var ta=document.querySelector('[name="'+name+'"]');
    if(ta) _rteMap[name]=new RichTextEditor(ta,null);
  });
  var ptTa=document.querySelector('[name="push_template"]');
  if(ptTa){_rteMap['push_template']=new RichTextEditor(ptTa,getPushTemplatePills);_rteMap['push_template'].refreshPills();}
  var btTa=document.querySelector('[name="broadcast_text"]');
  if(btTa){_rteMap['broadcast_text']=new RichTextEditor(btTa,getBroadcastPills);_rteMap['broadcast_text'].refreshPills();}

  // Broadcast Buttons Editor
  var broadcastBtnsRows=document.getElementById('broadcast-btn-rows');
  if(broadcastBtnsRows){
    function makeBroadcastRow(item){
      var row=document.createElement('div'); row.className='editor-row';
      var textIn=document.createElement('input');
      textIn.type='text'; textIn.placeholder='按钮文字'; textIn.value=item.text||'';
      textIn.dataset.field='text'; textIn.style.flex='1';
      var urlIn=document.createElement('input');
      urlIn.type='text'; urlIn.placeholder='链接 URL（https://...）'; urlIn.value=item.url||'';
      urlIn.dataset.field='url'; urlIn.style.flex='2';
      var rm=document.createElement('button');
      rm.type='button'; rm.textContent='✕'; rm.className='btn btn-danger btn-sm';
      rm.addEventListener('click',function(){row.remove();});
      row.appendChild(textIn); row.appendChild(urlIn); row.appendChild(rm);
      return row;
    }
    document.getElementById('broadcast-btn-add').addEventListener('click',function(){
      broadcastBtnsRows.appendChild(makeBroadcastRow({text:'',url:''}));
    });
    function serializeBroadcastBtns(){
      var result=[];
      broadcastBtnsRows.querySelectorAll('.editor-row').forEach(function(row){
        var text=row.querySelector('[data-field=text]').value.trim();
        var url=row.querySelector('[data-field=url]').value.trim();
        if(text&&url) result.push({text:text,url:url});
      });
      document.getElementById('broadcast_buttons_json').value=JSON.stringify(result);
    }
    document.getElementById('broadcast-form').addEventListener('submit',function(){
      if(_rteMap['broadcast_text'])_rteMap['broadcast_text'].sync();
      serializeBroadcastBtns();
      return confirm('确认向所有用户发送广播？');
    });
  }
  // ── Child bots management ──────────────────────────────────────────────
  function loadChildBots(){
    var container=document.getElementById('child-bots-list');
    if(!container) return;
    fetch('/admin/child-bots',{credentials:'include'}).then(function(r){return r.json();}).then(function(data){
      var bots=data.bots||[];
      if(!bots.length){container.innerHTML='<p style="color:#8b95b0;font-size:.88rem">暂无子机器人。</p>';return;}
      var html='<table class="table"><thead><tr><th>机器人</th><th>子管理员 ID</th><th>管理后台 URL</th><th>状态</th><th>添加时间</th><th>操作</th></tr></thead><tbody>';
      bots.forEach(function(b){
        var name=b.bot_name?(b.bot_name+(b.bot_username?' (@'+b.bot_username+')':'')):(b.bot_username?('@'+b.bot_username):'ID '+b.id);
        var running=b.running;
        var active=b.active;
        var ownerCell=b.owner_user_id?('<code style="font-size:.82rem">'+b.owner_user_id+'</code>'):'<em style="color:#94a3b8;font-size:.8rem">未设置</em>';
        var adminUrlCell=b.admin_panel_url?('<a href="'+b.admin_panel_url+'" target="_blank" style="font-size:.82rem;word-break:break-all">'+b.admin_panel_url+'</a>'):'<em style="color:#94a3b8;font-size:.8rem">未设置</em>';
        var statusBadge=running?'<span style="background:rgba(16,185,129,.15);color:#6ee7b7;border:1px solid rgba(16,185,129,.3);padding:2px 8px;border-radius:9px;font-size:.72rem;font-weight:700">✅ 运行中</span>':'<span style="background:rgba(244,63,94,.15);color:#fca5a5;border:1px solid rgba(244,63,94,.3);padding:2px 8px;border-radius:9px;font-size:.72rem;font-weight:700">⏹ 已停止</span>';
        var toggleLabel=active?'停用':'启用';
        html+='<tr><td>'+name+'</td><td>'+ownerCell+'</td><td>'+adminUrlCell+'</td><td>'+statusBadge+'</td><td style="white-space:nowrap;font-size:.82rem">'+((b.created_at||'').substring(0,19))+'</td><td style="white-space:nowrap"><button class="btn btn-secondary btn-sm" onclick="toggleChildBot('+b.id+','+(!active)+')" style="margin-right:4px">'+toggleLabel+'</button><button class="btn btn-danger btn-sm" onclick="removeChildBot('+b.id+')">删除</button></td></tr>';
      });
      html+='</tbody></table>';
      container.innerHTML=html;
    }).catch(function(){container.innerHTML='<p style="color:#ef4444;font-size:.85rem">加载失败，请刷新页面重试。</p>';});
  }
  window.removeChildBot=function(id){
    if(!confirm('确认删除该子机器人？机器人将立即停止。'))return;
    fetch('/admin/child-bots/remove',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({id:id})}).then(function(r){return r.json();}).then(function(){loadChildBots();}).catch(function(e){alert('删除失败：'+e);});
  };
  window.toggleChildBot=function(id,active){
    fetch('/admin/child-bots/toggle',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({id:id,active:active})}).then(function(r){return r.json();}).then(function(){loadChildBots();}).catch(function(e){alert('操作失败：'+e);});
  };
  (function(){
    var addBtn=document.getElementById('child-bot-add-btn');
    var tokenInput=document.getElementById('child-bot-token-input');
    var ownerInput=document.getElementById('child-bot-owner-input');
    var adminUrlInput=document.getElementById('child-bot-admin-url-input');
    var msg=document.getElementById('child-bot-add-msg');
    if(!addBtn) return;
    addBtn.addEventListener('click',function(){
      var token=(tokenInput.value||'').trim();
      var ownerRaw=(ownerInput?ownerInput.value||'':'').trim();
      var adminUrl=(adminUrlInput?adminUrlInput.value||'':'').trim();
      if(!token){msg.style.color='#ef4444';msg.textContent='请输入 Bot Token';return;}
      if(!ownerRaw||!/^\\d+$/.test(ownerRaw)){msg.style.color='#ef4444';msg.textContent='请输入子管理员的 Telegram 数字用户 ID';return;}
      addBtn.disabled=true;
      msg.style.color='#6b7280';
      msg.textContent='验证中，请稍候…';
      fetch('/admin/child-bots/add',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({token:token,owner_user_id:ownerRaw,admin_panel_url:adminUrl})}).then(function(r){return r.json().then(function(d){return{ok:r.ok,data:d};});}).then(function(res){
        addBtn.disabled=false;
        if(res.ok){
          msg.style.color='#16a34a';
          msg.textContent='✅ 已成功添加并启动：'+(res.data.bot_name||'')+(res.data.bot_username?' (@'+res.data.bot_username+')':'');
          tokenInput.value='';
          if(ownerInput)ownerInput.value='';
          if(adminUrlInput)adminUrlInput.value='';
          loadChildBots();
        } else {
          msg.style.color='#ef4444';
          msg.textContent='❌ '+(res.data.detail||'添加失败');
        }
      }).catch(function(e){addBtn.disabled=false;msg.style.color='#ef4444';msg.textContent='网络错误：'+e;});
    });
    if(document.querySelector('[data-tab="child-bots"]'))loadChildBots();
    document.querySelectorAll('.sub-btn').forEach(function(btn){
      if(btn.dataset.tab==='child-bots')btn.addEventListener('click',loadChildBots);
    });
  })();
})();
"""


def _render_report_content_for_admin(data_json: str, tpl_fields: list[dict[str, Any]]) -> str:
    """Return a short HTML snippet showing all field values of a report for admin review."""
    data = parse_json(data_json, {})
    if not data:
        return "<em style='color:#94a3b8'>（无内容）</em>"
    field_labels = {f["key"]: f["label"] for f in tpl_fields}
    field_types = {f["key"]: f.get("type", "text") for f in tpl_fields}
    parts = []
    for k, v in data.items():
        label = html.escape(field_labels.get(k, k))
        if field_types.get(k, "text") == "photo":
            parts.append(f"<b>{label}</b>：📷（图片，请在Telegram通知中查看）")
        else:
            display = html.escape(str(v)[:300])
            parts.append(f"<b>{label}</b>：{display}")
    return "<br>".join(parts) if parts else "<em style='color:#94a3b8'>（无内容）</em>"


def build_admin_html(settings_map: dict[str, str], pending_reports: list[dict] | None = None, saved: bool = False, user_count: int = 0, db_path: str = "", blacklist: list[dict] | None = None, all_reports: list[dict] | None = None, stats: dict | None = None, initial_tab: str = "basic", is_child_admin: bool = False) -> str:
    def e(key: str) -> str:
        return html.escape(settings_map.get(key, ""))

    def safe_js(key: str, fallback: Any) -> str:
        raw = settings_map.get(key, "")
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = fallback
        return (
            json.dumps(parsed, ensure_ascii=False)
            .replace("</", r"<\/")
            .replace("\u2028", r"\u2028")
            .replace("\u2029", r"\u2029")
        )

    start_buttons_js = safe_js("start_buttons_json", [])
    kb_buttons_js = safe_js("keyboard_buttons_json", [])
    template_js = safe_js("report_template_json", {"name": "", "fields": []})

    js = (
        _ADMIN_JS
        .replace("__START_BUTTONS__", start_buttons_js)
        .replace("__KB_BUTTONS__", kb_buttons_js)
        .replace("__TEMPLATE__", template_js)
    )

    pending_count = len(pending_reports) if pending_reports else 0
    pending_badge = f'<span class="badge">{pending_count}</span>' if pending_count > 0 else ""

    if pending_reports:
        tpl_fields = report_template()["fields"]
        all_pending_ids = ",".join(str(r["id"]) for r in pending_reports)
        rows_html = ""
        for r in pending_reports:
            content_html = _render_report_content_for_admin(r.get("data_json", "{}"), tpl_fields)
            rows_html += (
                "<tr>"
                f"<td>#{r['id']}</td>"
                f"<td>@{html.escape(r['username'] or 'unknown')}</td>"
                f"<td style='white-space:nowrap'>{html.escape(str(r['created_at'])[:19])}</td>"
                f"<td style='max-width:320px;word-break:break-word;font-size:.85rem;line-height:1.6'>{content_html}</td>"
                "<td style='white-space:nowrap;vertical-align:middle'>"
                f"<form method='post' action='/admin/approve/{r['id']}' style='display:block;margin-bottom:6px'>"
                "<button class='btn btn-success btn-sm' type='submit'>✅ 通过</button></form>"
                f"<form method='post' action='/admin/reject/{r['id']}' style='display:flex;gap:4px;align-items:center'>"
                "<input name='reason' placeholder='驳回原因' style='width:110px'>"
                "<button class='btn btn-danger btn-sm' type='submit'>❌ 驳回</button></form>"
                "</td></tr>"
            )
        pending_html = (
            f"<form method='post' action='/admin/batch-approve' style='margin-bottom:12px;display:flex;align-items:center;gap:10px'>"
            f"<input type='hidden' name='ids' value='{html.escape(all_pending_ids)}'>"
            f"<button class='btn btn-success' type='submit' onclick=\"return confirm('确认全部通过 {pending_count} 条待审核报告？')\">✅ 全部通过（{pending_count}条）</button>"
            "<a href='/admin#tab=pending' onclick='location.reload();return false;' style='font-size:.85rem;color:#93c5fd;text-decoration:none'>🔄 刷新列表</a>"
            "</form>"
            "<table class='table'><thead><tr>"
            "<th>ID</th><th>用户</th><th>提交时间</th><th>报告内容</th><th>操作</th>"
            "</tr></thead><tbody>" + rows_html + "</tbody></table>"
        )
    else:
        pending_html = "<p class='muted'>暂无待审核报告。</p>"

    # Build blacklist HTML
    if blacklist:
        bl_rows = ""
        for entry in blacklist:
            uid = html.escape(str(entry.get("user_id", "")))
            uname = html.escape(entry.get("username") or "")
            reason = html.escape(entry.get("reason") or "")
            added = html.escape(str(entry.get("added_at", ""))[:19])
            bl_rows += (
                "<tr>"
                f"<td>{uid}</td>"
                f"<td>{'@' + uname if uname else '<em style=\"color:#94a3b8\">未知</em>'}</td>"
                f"<td>{reason}</td>"
                f"<td style='white-space:nowrap'>{added}</td>"
                "<td>"
                f"<form method='post' action='/admin/blacklist/unban/{entry['user_id']}'>"
                "<button class='btn btn-success btn-sm' type='submit'>✅ 解除</button></form>"
                "</td></tr>"
            )
        blacklist_html = (
            "<table class='table'><thead><tr>"
            "<th>用户ID</th><th>用户名</th><th>原因</th><th>封禁时间</th><th>操作</th>"
            "</tr></thead><tbody>" + bl_rows + "</tbody></table>"
        )
    else:
        blacklist_html = "<p class='muted'>黑名单为空。</p>"

    # Build all reports HTML
    if all_reports:
        tpl_fields = report_template()["fields"]
        ar_rows = ""
        for r in all_reports:
            st = r.get("status", "")
            if st == "approved":
                badge = "<span style='background:rgba(16,185,129,.15);color:#6ee7b7;border:1px solid rgba(16,185,129,.3);padding:2px 8px;border-radius:9px;font-size:.72rem;font-weight:700'>✅ 已通过</span>"
            elif st == "rejected":
                badge = "<span style='background:rgba(244,63,94,.15);color:#fca5a5;border:1px solid rgba(244,63,94,.3);padding:2px 8px;border-radius:9px;font-size:.72rem;font-weight:700'>❌ 已驳回</span>"
            else:
                badge = "<span style='background:rgba(245,158,11,.15);color:#fde68a;border:1px solid rgba(245,158,11,.3);padding:2px 8px;border-radius:9px;font-size:.72rem;font-weight:700'>⏳ 待审核</span>"
            content_html = _render_report_content_for_admin(r.get("data_json", "{}"), tpl_fields)
            link_base = settings_map.get("report_link_base", "").strip()
            channel_link = r.get("channel_message_link") or ""
            if channel_link:
                detail_link = f"<a href='{html.escape(channel_link)}' target='_blank' style='color:var(--pri);text-decoration:none;font-size:.8rem'>频道链接 →</a>"
            elif link_base and st == "approved":
                web_url = f"{link_base.rstrip('/')}/reports/{r['id']}"
                detail_link = f"<a href='{html.escape(web_url)}' target='_blank' style='color:var(--pri);text-decoration:none;font-size:.8rem'>查看 →</a>"
            else:
                detail_link = ""
            feedback = html.escape(str(r.get("review_feedback") or ""))
            ar_rows += (
                "<tr>"
                f"<td>#{r['id']}</td>"
                f"<td>@{html.escape(r['username'] or 'unknown')}</td>"
                f"<td style='white-space:nowrap'>{html.escape(str(r.get('created_at',''))[:19])}</td>"
                f"<td>{badge}</td>"
                f"<td style='max-width:280px;word-break:break-word;font-size:.82rem;line-height:1.5'>{content_html}</td>"
                f"<td style='white-space:nowrap'>{detail_link}</td>"
                f"<td style='max-width:160px;word-break:break-word;font-size:.8rem;color:#8b95b0'>{feedback}</td>"
                "</tr>"
            )
        all_reports_html = (
            "<div style='overflow-x:auto'><table class='table'><thead><tr>"
            "<th>ID</th><th>用户</th><th>提交时间</th><th>状态</th><th>内容</th><th>链接</th><th>驳回原因</th>"
            "</tr></thead><tbody>" + ar_rows + "</tbody></table></div>"
        )
    else:
        all_reports_html = "<p class='muted'>暂无报告记录。</p>"

    saved_banner = "<div class='alert alert-success'>✅ 配置已保存成功！</div>" if saved else ""

    # For child-admin sessions show a notice and restrict the UI to report/blacklist tabs only.
    child_admin_banner = (
        "<div class='alert' style='background:rgba(245,158,11,.1);color:#fde68a;border:1px solid rgba(245,158,11,.25);border-left:4px solid #f59e0b'>"
        "⚠️ 您以子管理员身份登录，仅可查看和审核报告及黑名单，无权修改系统设置。"
        "</div>"
        if is_child_admin else ""
    )

    # Nav items that child admins are not allowed to see
    _hidden_if_child = "style='display:none'" if is_child_admin else ""
    # Override initial_tab to a visible tab for child admins
    if is_child_admin and initial_tab in _MAIN_ADMIN_ONLY_TABS:
        initial_tab = "pending"

    # Build stats bar
    _stats = stats or {}
    total_reports = _stats.get("total_reports", 0)
    approved_count = _stats.get("approved", 0)
    rejected_count = _stats.get("rejected", 0)

    pending_nav_badge = f"<span class='tab-badge'>{pending_count}</span>" if pending_count > 0 else ""

    # Stats overview for home tab
    stats_overview = f"""<div class="stats-row">
  <div class="stat-card"><div class="stat-num">{user_count}</div><div class="stat-lbl">用户</div></div>
  <div class="stat-card c-warn"><div class="stat-num">{pending_count}</div><div class="stat-lbl">待审核</div></div>
  <div class="stat-card c-suc"><div class="stat-num">{approved_count}</div><div class="stat-lbl">已通过</div></div>
  <div class="stat-card c-dan"><div class="stat-num">{rejected_count}</div><div class="stat-lbl">已驳回</div></div>
</div>"""

    media_types = [("", "无"), ("photo", "图片"), ("video", "视频")]
    current_media_type = settings_map.get("start_media_type", "").strip().lower()
    media_type_options = "".join(
        f"<option value='{v}'{' selected' if v == current_media_type else ''}>{label}</option>"
        for v, label in media_types
    )

    def _active_if(tab: str) -> str:
        """Return ' active' if this tab should be the initial visible pane."""
        return " active" if tab == initial_tab else ""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>报告机器人管理后台</title>
<style>{_ADMIN_CSS}</style>
</head>
<body>
<a class="skip-nav" href="#main-content">跳至主要内容</a>
<div class="layout">

<div class="main" id="main-content">

  <!-- ── Top Bar ─────────────────────────────────────── -->
  <header class="topbar">
    <div class="topbar-left">
      <span class="topbar-icon" id="topbar-icon" aria-hidden="true">⏳</span>
      <span class="topbar-title" id="topbar-title">待审核报告</span>
    </div>
    <div class="topbar-right">
      <span class="topbar-stat" aria-label="{user_count} 位用户">{user_count} 位用户</span>
      <a href="/admin/logout" class="topbar-logout" aria-label="退出登录">退出</a>
    </div>
  </header>

  <!-- ── Sub-nav: Settings ───────────────────────────── -->
  <div class="sub-nav" id="sub-nav-settings" role="tablist" aria-label="设置子导航">
    <button class="sub-btn active" data-tab="basic" role="tab" {_hidden_if_child}>⚙️ 基本</button>
    <button class="sub-btn" data-tab="welcome" role="tab" {_hidden_if_child}>👋 欢迎</button>
    <button class="sub-btn" data-tab="keyboard" role="tab" {_hidden_if_child}>⌨️ 菜单</button>
    <button class="sub-btn" data-tab="template" role="tab" {_hidden_if_child}>📝 模板</button>
    <button class="sub-btn" data-tab="texts" role="tab" {_hidden_if_child}>💬 文本</button>
    <button class="sub-btn" data-tab="review" role="tab" {_hidden_if_child}>🔍 审核</button>
    <button class="sub-btn" data-tab="child-bots" role="tab" {_hidden_if_child}>🤖 子机器人</button>
  </div>

  <!-- ── Sub-nav: Users ─────────────────────────────── -->
  <div class="sub-nav" id="sub-nav-users" role="tablist" aria-label="用户管理子导航">
    <button class="sub-btn active" data-tab="blacklist" role="tab">🚫 黑名单</button>
    <button class="sub-btn" data-tab="broadcast" role="tab" {_hidden_if_child}>📢 广播</button>
  </div>

  <!-- ── Content ────────────────────────────────────── -->
  <div class="content">
    {child_admin_banner}
    {saved_banner}

    <form id="settings-form" method="post" action="/admin/save">

    <div id="pane-basic" class="tab-pane{_active_if('basic')}">
      <p class="section-title">基本设置</p>
      <div class="card">
        <div class="field-row">
          <div class="field">
            <label>强制订阅频道</label>
            <input type="text" name="force_sub_channel" value="{e('force_sub_channel')}" placeholder="@频道用户名">
            <div class="hint">填 @用户名，用户须先订阅该频道才能使用机器人（留空则不限制）</div>
          </div>
          <div class="field">
            <label>报告推送频道</label>
            <input type="text" name="push_channel" value="{e('push_channel')}" placeholder="@频道用户名">
            <div class="hint">审核通过的报告自动推送到该频道（留空则不推送）</div>
          </div>
        </div>
        <div class="field">
          <label>报告链接基地址</label>
          <input type="text" name="report_link_base" value="{e('report_link_base')}" placeholder="https://yourdomain.com">
          <div class="hint">报告查询结果显示链接的前缀，链接格式为：域名/reports/ID（留空则仅显示报告 ID）；当推送到频道时会自动使用频道消息链接，无需另行配置</div>
        </div>
        <div class="field">
          <label>数据库</label>
          <input type="text" value="PostgreSQL (DATABASE_URL)" readonly>
          <div class="hint">数据库使用 PostgreSQL，数据持久化存储，重新部署不会丢失。请确保在平台环境变量中设置 <code>DATABASE_URL</code>。</div>
        </div>
        <div class="field">
          <label>配置导出 / 导入</label>
          <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start">
            <a href="/admin/export-settings" class="btn btn-secondary" style="text-decoration:none">⬇️ 导出配置 JSON</a>
            <div style="display:flex;gap:6px;align-items:flex-start;flex-wrap:wrap">
              <textarea name="settings_json" rows="3" placeholder="粘贴之前导出的配置 JSON..." style="width:300px;min-width:200px;font-size:.84rem;resize:vertical" form="import-settings-form"></textarea>
              <button type="submit" class="btn btn-success" onclick="return confirm('导入将覆盖现有配置，确认吗？')" form="import-settings-form">⬆️ 导入配置</button>
            </div>
          </div>
          <div class="hint" style="margin-top:6px">可将当前配置导出为 JSON 文件保存备份；重新部署后可导入恢复设置。</div>
        </div>
      </div>
    </div>

    <div id="pane-welcome" class="tab-pane">
      <p class="section-title">欢迎消息（/start 命令）</p>
      <div class="card">
        <div class="field">
          <label>/start 欢迎文本</label>
          <textarea name="start_text" rows="4">{e('start_text')}</textarea>
          <div class="hint">使用工具栏进行格式化；支持 Telegram HTML：加粗、斜体、下划线、链接等</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>媒体类型</label>
            <select name="start_media_type">
              {media_type_options}
            </select>
            <div class="hint">选择后需在右侧填写对应的媒体 URL</div>
          </div>
          <div class="field">
            <label>媒体 URL</label>
            <input type="text" name="start_media_url" value="{e('start_media_url')}" placeholder="https://...">
            <div class="hint">图片或视频的直链地址</div>
          </div>
        </div>
        <div class="field">
          <label>欢迎消息内联按钮</label>
          <div class="hint" style="margin-bottom:8px">显示在欢迎文字下方的按钮，点击后跳转链接</div>
          <div id="start-btn-rows"></div>
          <button type="button" class="btn-add" id="start-btn-add">＋ 添加按钮</button>
          <input type="hidden" name="start_buttons_json" id="start_buttons_json">
        </div>
      </div>
    </div>

    <div id="pane-keyboard" class="tab-pane">
      <p class="section-title">底部快捷键盘</p>
      <div class="card">
        <div class="hint" style="margin-bottom:14px">配置用户输入框下方的快捷按钮。可绑定内置功能，也可自定义回复内容。"行号"相同的按钮将显示在同一行（留空则独占一行）。</div>
        <div id="kb-rows"></div>
        <button type="button" class="btn-add" id="kb-add">＋ 添加按钮</button>
        <input type="hidden" name="keyboard_buttons_json" id="keyboard_buttons_json">
      </div>
    </div>

    <div id="pane-template" class="tab-pane">
      <p class="section-title">报告填写模板</p>
      <div class="card">
        <div class="field">
          <label>模板名称</label>
          <input type="text" id="template-name" placeholder="例如：日报">
          <input type="hidden" name="report_template_json" id="report_template_json">
        </div>
        <div class="field">
          <label>模板字段</label>
          <div class="hint" style="margin-bottom:10px">每个字段可设置：英文标识（键名）、显示名称、类型（文本/图片）、是否必填、字段说明（提示用户如何填写）</div>
          <div id="template-fields"></div>
          <button type="button" class="btn-add" id="template-add">＋ 添加字段</button>
        </div>
      </div>
    </div>

    <div id="pane-texts" class="tab-pane">
      <p class="section-title">功能文本配置</p>
      <div class="card">
        <div class="field">
          <label>查阅报告 — 帮助文本</label>
          <textarea name="search_help_text" rows="3">{e('search_help_text')}</textarea>
          <div class="hint">用户点击「查阅报告」后显示的提示，说明如何使用 @用户名 或 #标签 搜索</div>
        </div>
        <div class="field">
          <label>联系管理员 — 文本</label>
          <textarea name="contact_text" rows="3">{e('contact_text')}</textarea>
        </div>
        <div class="field">
          <label>操作方式 — 说明文本</label>
          <textarea name="usage_text" rows="5">{e('usage_text')}</textarea>
        </div>
      </div>
    </div>

    <div id="pane-review" class="tab-pane">
      <p class="section-title">审核反馈通知</p>
      <div class="card">
        <div class="field">
          <label>审核通过 — 通知模板</label>
          <input type="text" name="review_approved_template" value="{e('review_approved_template')}">
          <div class="hint">使用 {{id}} 表示报告编号，{{link}} 表示报告链接，例如：✅ 报告 #{{id}} 审核通过。{{link}}</div>
        </div>
        <div class="field">
          <label>审核驳回 — 通知模板</label>
          <input type="text" name="review_rejected_template" value="{e('review_rejected_template')}">
          <div class="hint">使用 {{id}} 表示编号，{{reason}} 表示驳回原因，例如：❌ 报告 #{{id}} 未通过：{{reason}}</div>
        </div>
        <div class="field">
          <label>推送频道 — 推送模板</label>
          <textarea name="push_template" rows="4">{e('push_template')}</textarea>
          <div class="hint">支持占位符：{{id}} 报告编号、{{username}} 用户名、{{detail}} 报告字段内容、{{link}} 报告链接；点击上方字段按钮快速插入。<br>还可直接使用字段键名，如模板含 <code>title</code> 字段则可用 {{{{title}}}}（前后各两个大括号）。</div>
        </div>
        <div class="field">
          <label>推送图片 — 开关</label>
          <label style="display:flex;align-items:center;gap:8px;font-weight:normal;font-size:.88rem;cursor:pointer;text-transform:none;letter-spacing:0;color:var(--txt)">
            <input type="checkbox" name="push_photos_enabled" value="1"{'checked' if settings_map.get('push_photos_enabled','1') == '1' else ''}>
            审核通过后，将报告中的图片字段也推送到频道
          </label>
          <div class="hint" style="margin-top:4px">开启后，文字推送完成后会依次发送图片字段；关闭则仅推送文字内容。</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>待审提醒 — 触发阈值（小时）</label>
            <input type="number" name="pending_reminder_threshold_hours" value="{e('pending_reminder_threshold_hours') or '24'}" min="1" max="720" style="width:100px">
            <div class="hint">报告待审超过此时长（小时）后向管理员发送提醒，默认 24。</div>
          </div>
          <div class="field">
            <label>待审提醒 — 检查间隔（小时）</label>
            <input type="number" name="pending_reminder_interval_hours" value="{e('pending_reminder_interval_hours') or '2'}" min="1" max="168" style="width:100px">
            <div class="hint">每隔多少小时触发一次检查，默认 2。修改后需重启 Bot 生效。</div>
          </div>
        </div>
      </div>
    </div>

    <div class="save-bar" id="settings-save-bar">
      <button type="submit" class="btn btn-primary">💾 保存配置</button>
    </div>

    </form>
    <form id="import-settings-form" method="post" action="/admin/import-settings"></form>

    <div id="pane-pending" class="tab-pane{_active_if('pending')}">
      {stats_overview}
      <p class="section-title">待审核报告（{pending_count} 条）</p>
      <div class="card" style="padding:0;overflow:hidden">
        <div style="padding:12px 16px;border-bottom:1px solid var(--bdr);display:flex;align-items:center;justify-content:space-between">
          <span style="font-size:.83rem;color:var(--txt2)">共 {pending_count} 条待审核</span>
          <a href="/admin#tab=pending" onclick="location.reload();return false;" style="font-size:.82rem;color:var(--pri);text-decoration:none">🔄 刷新</a>
        </div>
        <div style="overflow-x:auto">{pending_html}</div>
      </div>
    </div>

    <div id="pane-reports" class="tab-pane">
      <p class="section-title">报告历史（共 {total_reports} 条）</p>
      <div class="card" style="padding:0;overflow:hidden">
        <div style="padding:14px 18px;border-bottom:1px solid var(--bdr);display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <span style="font-size:.85rem;color:var(--txt2)">
            ✅ 已通过 {approved_count} &nbsp; ❌ 已驳回 {rejected_count} &nbsp; ⏳ 待审核 {pending_count}
          </span>
          <a href="/admin/export-reports" class="btn btn-secondary btn-sm" style="text-decoration:none;margin-left:auto">⬇️ 导出 CSV</a>
        </div>
        {all_reports_html}
      </div>
    </div>

    <div id="pane-blacklist" class="tab-pane">
      <p class="section-title">黑名单管理</p>
      <div class="card">
        <form method="post" action="/admin/blacklist/ban" style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:20px;padding-bottom:18px;border-bottom:1px solid var(--bdr)">
          <div>
            <label>用户 ID</label>
            <input type="text" name="user_id" placeholder="数字用户ID" style="width:140px">
          </div>
          <div>
            <label>原因（可选）</label>
            <input type="text" name="reason" placeholder="限制原因" style="width:200px">
          </div>
          <button type="submit" class="btn btn-danger" style="margin-bottom:1px">🚫 加入黑名单</button>
        </form>
        <div style="overflow-x:auto">{blacklist_html}</div>
      </div>
    </div>

    <div id="pane-broadcast" class="tab-pane">
      <p class="section-title">广播发送</p>
      <div class="card">
        <div style="margin-bottom:16px;padding:12px 16px;background:rgba(99,102,241,.1);border-radius:var(--r);font-size:.84rem;color:#a5b4fc;border:1px solid rgba(99,102,241,.22)">
          📊 共 <b>{user_count}</b> 位用户曾使用机器人
        </div>
        <form id="broadcast-form" method="post" action="/admin/broadcast">
          <div class="field">
            <label>广播文本</label>
            <textarea name="broadcast_text" rows="5" placeholder="使用工具栏格式化文字；点击字段按钮快速插入模板字段内容"></textarea>
          </div>
          <div class="field-row">
            <div class="field">
              <label>媒体类型</label>
              <select name="broadcast_media_type">
                <option value="">无</option>
                <option value="photo">图片</option>
                <option value="video">视频</option>
              </select>
              <div class="hint">选择后需在右侧填写对应的媒体 URL</div>
            </div>
            <div class="field">
              <label>媒体 URL</label>
              <input type="text" name="broadcast_media_url" placeholder="https://...">
              <div class="hint">图片或视频的直链地址</div>
            </div>
          </div>
          <div class="field">
            <label>内联按钮（可选）</label>
            <div class="hint" style="margin-bottom:8px">每行一个按钮，点击后跳转链接</div>
            <div id="broadcast-btn-rows"></div>
            <button type="button" class="btn-add" id="broadcast-btn-add">＋ 添加按钮</button>
            <input type="hidden" name="broadcast_buttons_json" id="broadcast_buttons_json">
          </div>
          <div style="margin-top:16px">
            <button type="submit" class="btn btn-primary">📢 发送广播</button>
          </div>
        </form>
      </div>
    </div>

    <div id="pane-child-bots" class="tab-pane">
      <p class="section-title">子机器人管理</p>
      <div class="card">
        <div style="margin-bottom:16px;padding:14px 16px;background:rgba(99,102,241,.08);border-radius:var(--r2);border:1px solid rgba(99,102,241,.2);font-size:.84rem;color:#93c5fd;line-height:1.8">
          <b>📖 使用说明</b><br>
          1️⃣ 在 @BotFather 创建新机器人，获得 Bot Token<br>
          2️⃣ 将该 Token 及其 Telegram 用户 ID 填入下方，点击「添加」<br>
          3️⃣ 系统立即启动子机器人，仅该子管理员可使用管理命令<br>
          子机器人是一款全新的独立机器人，拥有自己独立的设置，与主机器人互不影响。
        </div>
        <div class="field">
          <label>Bot Token</label>
          <input type="text" id="child-bot-token-input" placeholder="粘贴 Bot Token（例如 123456:ABC…）">
          <div class="hint">Token 来自 @BotFather。</div>
        </div>
        <div class="field">
          <label>子管理员 Telegram 用户 ID</label>
          <input type="text" id="child-bot-owner-input" placeholder="例如 123456789（必填）" inputmode="numeric">
          <div class="hint">子机器人的管理员的 Telegram 数字 ID。只有该用户才能使用 /admin、/pending、/approve 等管理命令。可通过 @userinfobot 获取。</div>
        </div>
        <div class="field">
          <label>子机器人管理后台 URL（可选）</label>
          <input type="url" id="child-bot-admin-url-input" placeholder="例如 https://my-child-bot.up.railway.app">
          <div class="hint">子机器人自己的管理后台地址。填写后，子管理员点击机器人中的「管理后台」按钮将进入此地址，而非主机器人的管理后台。</div>
        </div>
        <div style="margin-top:8px">
          <button type="button" class="btn btn-success" id="child-bot-add-btn">＋ 添加并启动</button>
          <div id="child-bot-add-msg" style="margin-top:6px;font-size:.85rem"></div>
        </div>
        <div class="field" style="margin-top:20px">
          <label>已注册的子机器人</label>
          <div id="child-bots-list" style="margin-top:8px">
            <div style="color:#8b95b0;font-size:.88rem">加载中…</div>
          </div>
        </div>
      </div>
    </div>

  </div><!-- .content -->
</div><!-- .main -->

<!-- ── Bottom Tab Bar ───────────────────────────────── -->
<nav class="tab-bar" role="tablist" aria-label="主导航">
  <button type="button" class="tab-btn active" data-section="home" role="tab" aria-selected="true" aria-label="首页">
    <span class="tab-icon" aria-hidden="true">🏠</span>
    <span class="tab-label">首页</span>
    {pending_nav_badge}
  </button>
  <button type="button" class="tab-btn" data-section="users" role="tab" aria-selected="false" aria-label="用户管理">
    <span class="tab-icon" aria-hidden="true">👥</span>
    <span class="tab-label">用户</span>
  </button>
  <button type="button" class="tab-btn" data-section="stats" role="tab" aria-selected="false" aria-label="报告统计">
    <span class="tab-icon" aria-hidden="true">📊</span>
    <span class="tab-label">统计</span>
  </button>
  <button type="button" class="tab-btn" data-section="settings" role="tab" aria-selected="false" aria-label="系统设置" {_hidden_if_child}>
    <span class="tab-icon" aria-hidden="true">⚙️</span>
    <span class="tab-label">设置</span>
  </button>
</nav>

</div><!-- .layout -->
<script>{js}</script>
</body>
</html>
"""
