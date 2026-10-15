/* Errands docs — install tabs, transcript replay */

"use strict";

const CMDS = {
  win: {
    cmd: "irm https://raw.githubusercontent.com/Sachitt-AV-08/errands/main/install.ps1 | iex",
    note: "Installs the v0.1.0 release &mdash; 111 tests, 89% coverage gate.",
  },
  unix: {
    cmd: "curl -fsSL https://raw.githubusercontent.com/Sachitt-AV-08/errands/main/install.sh | sh",
    note: "Installs the v0.1.0 release &mdash; 111 tests, 89% coverage gate.",
  },
  src: {
    cmd: "git clone https://github.com/Sachitt-AV-08/errands\ncd errands && uv sync",
    note: "Runs from source, so you get all changes on <code>main</code>.",
  },
};

function initInstallTabs(containerSelector, outId, noteId) {
  const out = document.getElementById(outId);
  const note = document.getElementById(noteId);
  const container = document.querySelector(containerSelector);
  if (!container || !out || !note) return;

  const tabs = Array.from(container.querySelectorAll(".tab"));

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => {
        t.classList.remove("active");
        t.setAttribute("aria-selected", "false");
      });
      tab.classList.add("active");
      tab.setAttribute("aria-selected", "true");
      const os = tab.dataset.os;
      out.textContent = CMDS[os].cmd;
      note.innerHTML = CMDS[os].note;
    });
  });
}

async function renderTranscript() {
  const gateTerm = document.getElementById("term-gate");
  const gateTermHero = document.getElementById("term-gate-hero");

  if (!gateTerm && !gateTermHero) return;

  try {
    const resp = await fetch("transcript.json", { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    const gate = data.gate;
    if (!gate) {
      if (gateTerm) gateTerm.textContent = "transcript.json missing gate entry";
      if (gateTermHero) gateTermHero.textContent = "transcript.json missing gate entry";
      return;
    }

    let html = "";
    if (gate.transcript) {
      html = gate.transcript
        .split("\n")
        .map((line) => {
          if (line.includes("held") || line.includes("needs_human") || line.includes("not permitted")) {
            return `<span class="unverified">${escapeHtml(line)}</span>`;
          }
          if (line.includes("ok: true") || line.includes("verified")) {
            return `<span class="ok">${escapeHtml(line)}</span>`;
          }
          return escapeHtml(line);
        })
        .join("\n");
    } else {
      html = `tool: ${gate.tool}\nargs: ${JSON.stringify(gate.args)}\nallowed: ${gate.allowed}\nrisk: ${gate.risk}\nreason: ${gate.reason}`;
    }
    if (gateTerm) gateTerm.innerHTML = html;
    if (gateTermHero) gateTermHero.innerHTML = html;
  } catch (e) {
    const msg = "transcript unavailable — " + e.message;
    if (gateTerm) {
      gateTerm.textContent = msg;
      gateTerm.style.color = "var(--bad)";
    }
    if (gateTermHero) {
      gateTermHero.textContent = msg;
      gateTermHero.style.color = "var(--bad)";
    }
  }
}

function escapeHtml(s) {
  return s
    .replace(/&/g, "&")
    .replace(/</g, "<")
    .replace(/>/g, ">")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function initReveal() {
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.1, rootMargin: "0px 0px -50px 0px" }
  );
  document.querySelectorAll(".reveal").forEach((el) => observer.observe(el));
}

document.addEventListener("DOMContentLoaded", () => {
  initInstallTabs("#install .installbox", "installcmd", "installnote");
  initInstallTabs(".hero .installbox", "installcmd-hero", "installnote-hero");
  renderTranscript();
  initReveal();
});