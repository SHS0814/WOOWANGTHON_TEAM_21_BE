"""라벨 확인 시트(로컬 HTML)를 만든다. 사용자가 사진을 한 장씩 보고 칸별 정답을 고친 뒤 labels.json 으로 내보낸다.

    uv run python -m harness.review_sheet harness/data/real/labels.draft.json

라벨 파일과 같은 폴더에 review.html 을 쓴다 (사진 경로가 라벨 파일 기준 상대 경로라서).
시트에는 라벨 데이터가 그대로 들어가므로 이 HTML 도 개인정보다. harness/data/ 밖으로 옮기지 않는다.
브라우저에서 파일로 열면 되고 서버나 네트워크는 쓰지 않는다. 진행 상황은 브라우저 저장소에 임시 저장된다.
"""

import argparse
import json
from pathlib import Path

from harness.labels import load_labels

TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>약통 라벨 확인</title>
<style>
:root { --bg:#f6f6f4; --card:#fff; --ink:#1d1d1b; --mute:#6b6b66; --line:#d9d9d4;
  --full:#1f5fbf; --empty:#8a8a84; --hidden:#b8860b; --check:#e07000; --ok:#2e7d32; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
header { position:sticky; top:0; z-index:5; background:var(--card); border-bottom:1px solid var(--line);
  padding:10px 16px; display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
header b { margin-right:auto; }
button, label.btn { font:inherit; border:1px solid var(--line); background:#fff; border-radius:6px;
  padding:6px 10px; cursor:pointer; }
button.primary { background:var(--ink); color:#fff; border-color:var(--ink); }
main { max-width:1100px; margin:0 auto; padding:16px; }
.notice { background:#fff7e6; border:1px solid #f0c36d; border-radius:8px; padding:12px 14px; margin-bottom:16px; }
.notice p { margin:4px 0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px;
  margin-bottom:16px; }
.card.confirmed { border-color:var(--ok); box-shadow:0 0 0 1px var(--ok) inset; }
.card h2 { font-size:16px; margin:0 0 4px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.meta { color:var(--mute); font-size:13px; margin-bottom:8px; }
.photo { display:block; max-width:100%; max-height:520px; margin:0 auto 10px; border-radius:6px;
  background:#eee; }
.grid { display:grid; gap:6px; margin-bottom:10px; }
.cell { border:2px solid var(--line); border-radius:8px; padding:6px 4px; text-align:center; cursor:pointer;
  background:#fff; user-select:none; min-width:0; }
.cell .lab { font-size:18px; font-weight:600; }
.cell .st { font-size:12px; font-weight:600; }
.cell.full .st { color:var(--full); } .cell.full { background:#eaf1fc; }
.cell.empty .st { color:var(--empty); }
.cell.not_visible .st { color:var(--hidden); }
.cell.not_visible { background:repeating-linear-gradient(45deg,#fff,#fff 6px,#f6efdc 6px,#f6efdc 12px); }
.cell.check { border-color:var(--check); border-style:dashed; }
.cell .tag { display:block; font-size:11px; color:var(--check); }
.cell .chg { display:block; font-size:11px; color:var(--ok); }
.row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
textarea { width:100%; font:inherit; border:1px solid var(--line); border-radius:6px; padding:6px; min-height:2.4em; }
.pill { font-size:12px; border-radius:999px; padding:1px 8px; background:#eee; color:var(--mute); }
.pill.ok { background:#e5f3e6; color:var(--ok); }
.legend span { margin-right:12px; }
.hide { display:none; }
</style>
</head>
<body>
<header>
  <b>약통 라벨 확인 <span id="progress"></span></b>
  <label class="btn"><input type="checkbox" id="onlyTodo"> 확인 안 한 것만</label>
  <label class="btn">불러오기 <input type="file" id="importFile" accept=".json" class="hide"></label>
  <button id="reset">초안으로 되돌리기</button>
  <button class="primary" id="export">labels.json 내보내기</button>
</header>
<main>
  <div class="notice">
    <p><b>칸별 초안은 AI 에이전트(Claude)가 사진을 눈으로 보고 만든 것이다. 아직 정답이 아니다.</b></p>
    <p>Claude는 평가 대상 모델과 같은 계열이라, 초안을 그대로 승인하면 결과가 Claude 쪽으로 치우칠 수 있다(순환).
      모든 칸을 직접 보고 맞는지 확인해 달라. 확인하지 않은 사진은 <code>draft</code>로 남고, 하네스는 draft 라벨로 낸 숫자를 결과로 쓰지 않는다.</p>
    <p>칸을 누르면 <b>약 있음 → 빔 → 안 보임</b> 순서로 바뀐다. "안 보임"은 사진에 없거나 잘려서 칸 안쪽을 볼 수 없는 칸이다.
      <span style="color:var(--check)">주황 점선</span>은 초안 작성자가 확신하지 못한 칸이다. 다 봤으면 "이 사진 확인함"을 체크한다.</p>
    <p>끝나면 "labels.json 내보내기"로 받은 파일을 이 시트와 같은 폴더(<code>__DIR__</code>)에 <code>labels.json</code>으로 둔다.
      진행 상황은 이 브라우저에만 임시 저장된다. 중간에 내보내 두면 "불러오기"로 이어서 할 수 있다.</p>
    <p class="legend"><span style="color:var(--full)">■ 약 있음(full)</span><span style="color:var(--empty)">■ 빔(empty)</span><span style="color:var(--hidden)">■ 안 보임(not_visible)</span></p>
  </div>
  <div id="list"></div>
</main>
<script id="draft" type="application/json">__DATA__</script>
<script>
const DRAFT = JSON.parse(document.getElementById("draft").textContent);
const KEY = "label-review:" + __KEY__;
const NEXT = { full: "empty", empty: "not_visible", not_visible: "full" };
const NAME = { full: "약 있음", empty: "빔", not_visible: "안 보임" };
const draftById = Object.fromEntries(DRAFT.photos.map(p => [p.id, p]));

function fresh() {
  return Object.fromEntries(DRAFT.photos.map(p => [p.id, {
    cells: { ...p.cells }, confirmed: p.label_status === "confirmed", note: "" }]));
}
let state = fresh();
try {
  const saved = JSON.parse(localStorage.getItem(KEY) || "null");
  if (saved) for (const id in saved) if (state[id]) state[id] = saved[id];
} catch (e) { /* 저장소를 못 쓰면 이번 창에서만 기억한다 */ }

function save() {
  try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
  render();
}

function cellIds(layout) {
  const ids = [];
  for (let r = 0; r < layout.rows; r++) for (let c = 0; c < layout.cols; c++) ids.push([r, c]);
  return ids;
}

function render() {
  const onlyTodo = document.getElementById("onlyTodo").checked;
  const done = DRAFT.photos.filter(p => state[p.id].confirmed).length;
  document.getElementById("progress").textContent = `(확인 ${done}/${DRAFT.photos.length})`;
  const list = document.getElementById("list");
  list.innerHTML = "";
  for (const p of DRAFT.photos) {
    const s = state[p.id];
    if (onlyTodo && s.confirmed) continue;
    const L = p.pillbox.layout;
    const card = document.createElement("section");
    card.className = "card" + (s.confirmed ? " confirmed" : "");
    const cond = Object.entries(p.conditions || {}).map(([k, v]) => `${k}=${v}`).join(", ");
    card.innerHTML = `<h2>${p.id} <span class="pill ${s.confirmed ? "ok" : ""}">${s.confirmed ? "확인함" : "초안"}</span></h2>
      <div class="meta"></div>
      <a href="${p.file}" target="_blank"><img class="photo" loading="lazy" src="${p.file}" alt="${p.id}"></a>
      <div class="grid" style="grid-template-columns:repeat(${L.cols}, minmax(0,1fr))"></div>
      <div class="row"><label><input type="checkbox" class="ok"> 이 사진 확인함</label></div>
      <textarea class="note" placeholder="메모 (선택): 고친 이유, 애매한 점"></textarea>`;
    card.querySelector(".meta").textContent =
      [p.pillbox.name, cond, p.notes ? "초안 메모: " + p.notes : ""].filter(Boolean).join(" · ");
    const grid = card.querySelector(".grid");
    for (const [r, c] of cellIds(L)) {
      const id = `r${r}c${c}`;
      const v = s.cells[id];
      const el = document.createElement("div");
      const check = (p.check_cells || []).includes(id);
      el.className = `cell ${v}` + (check ? " check" : "");
      const lab = [L.row_labels[r], L.col_labels[c]].filter(Boolean).join(" ") || id;
      const changed = draftById[p.id].cells[id] !== v;
      el.innerHTML = `<div class="lab"></div><div class="st">${NAME[v]}</div>` +
        (check ? `<span class="tag">확인 필요</span>` : "") +
        (changed ? `<span class="chg">초안에서 고침</span>` : "");
      el.querySelector(".lab").textContent = lab;
      el.title = id;
      el.onclick = () => { s.cells[id] = NEXT[v]; save(); };
      grid.appendChild(el);
    }
    const ok = card.querySelector(".ok");
    ok.checked = s.confirmed;
    ok.onchange = () => { s.confirmed = ok.checked; save(); };
    const note = card.querySelector(".note");
    note.value = s.note || "";
    note.onchange = () => { s.note = note.value; save(); };
    list.appendChild(card);
  }
}

document.getElementById("onlyTodo").onchange = render;
document.getElementById("reset").onclick = () => {
  if (confirm("고친 내용과 확인 표시를 모두 지우고 초안으로 되돌릴까요?")) { state = fresh(); save(); }
};
document.getElementById("export").onclick = () => {
  const out = {
    version: DRAFT.version,
    drafted_by: DRAFT.drafted_by || "",
    confirmed_with: "harness/review_sheet.py 확인 시트",
    exported_at: new Date().toISOString(),
    photos: DRAFT.photos.map(p => {
      const s = state[p.id];
      const notes = [p.notes, s.note ? "확인 메모: " + s.note : ""].filter(Boolean).join(" / ");
      return { ...p, cells: { ...s.cells }, label_status: s.confirmed ? "confirmed" : "draft", notes };
    }),
  };
  const blob = new Blob([JSON.stringify(out, null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "labels.json";
  a.click();
  URL.revokeObjectURL(a.href);
};
document.getElementById("importFile").onchange = async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    let n = 0;
    for (const p of data.photos || []) {
      if (!state[p.id]) continue;
      const note = (p.notes || "").split("확인 메모: ")[1] || "";
      state[p.id] = { cells: { ...p.cells }, confirmed: p.label_status === "confirmed", note };
      n++;
    }
    save();
    alert(`${n}장을 불러왔습니다.`);
  } catch (e) { alert("불러오지 못했습니다: " + e.message); }
  ev.target.value = "";
};
render();
</script>
</body>
</html>
"""


def build(labels_path: Path, out: Path) -> None:
    labels, _ = load_labels(labels_path)  # 형식 검증
    data = json.loads(labels_path.read_text(encoding="utf-8"))
    # <script> 안에 넣으므로 "</" 를 끊는다
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = (
        TEMPLATE.replace("__DATA__", payload)
        .replace("__KEY__", json.dumps(labels_path.name))
        .replace("__DIR__", str(labels_path.parent))
    )
    out.write_text(html, encoding="utf-8")
    print(f"{out} ({len(labels.photos)}장, 초안 {len(labels.drafts())}장)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("labels", type=Path, help="초안 라벨 파일 (labels.draft.json 등)")
    ap.add_argument("--out", type=Path, help="기본: 라벨 파일과 같은 폴더의 review.html")
    args = ap.parse_args(argv)
    build(args.labels, args.out or args.labels.parent / "review.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
