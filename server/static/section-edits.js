(() => {
  "use strict";

  function create({ state, api, currentProjectView, setDraftSaveStatus, updateDraftStats }) {
    // Each editor keeps the exact server text on which its local edit was based.
    // Keep dirty or in-flight entries across workspace renders, within this tab and account.
    const sectionEdits = new Map();

    function open(key, remoteContent) {
      const draft = sectionEdits.get(key) || { base: remoteContent, content: remoteContent, pending: 0 };
      draft.remote = remoteContent;
      if (draft.content === remoteContent) draft.base = remoteContent;
      sectionEdits.set(key, draft);
      return draft;
    }

    function detachEditors() {
      for (const [key, edit] of sectionEdits) {
        edit.display = null;
        if (!edit.pending && edit.content === edit.base && edit.remote === edit.base) sectionEdits.delete(key);
      }
    }

    function removeProject(projectId) {
      for (const key of sectionEdits.keys()) {
        if (key.startsWith(`${projectId}\0`)) sectionEdits.delete(key);
      }
    }

    async function writeSection(id, sid, base, content, revision) {
      if (!Number.isSafeInteger(revision) || revision < 0) throw new Error("未取得项目版本，请刷新项目后重试");
      const saved = await api(`/api/projects/${id}/sections/${sid}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, base_content: base, expected_revision: revision }),
      });
      if (!Number.isSafeInteger(saved?.material_revision)) throw new Error("保存结果待确认，请核对最新正文");
      return saved.material_revision;
    }

    async function confirmGeneratedSection(view, sid, base, result) {
      if (result.material_applied) return result.material_revision;
      const latest = await api(`/api/projects/${view.id}`);
      if (!view.active()) throw new Error("项目已切换");
      if (!latest.outline?.some(item => item.id === sid)) throw new Error("章节已移出当前目录");
      const remote = latest.sections?.[sid] || "";
      if (remote === result.content) return latest.material_revision;
      if (remote !== base) throw new Error("其他页面已修改正文，请核对生成草稿");
      return writeSection(view.id, sid, base, result.content, latest.material_revision);
    }

    function applyGeneratedSection(sid, content, revision) {
      if (revision < state.project.material_revision && state.project.sections?.[sid] !== content) return false;
      state.project.sections ||= {};
      state.project.sections[sid] = content;
      if (revision !== undefined) state.project.material_revision = Math.max(revision, state.project.material_revision ?? 0);
      const draft = sectionEdits.get(`${state.project.id}\0${sid}`);
      if (draft) {
        const current = draft.display?.editor.value ?? draft.content;
        draft.content = current === draft.base ? content : current;
        draft.base = draft.remote = content;
        if (draft.display) {
          draft.display.editor.value = draft.content;
          draft.display.paint();
        }
      }
      return true;
    }

    async function flushDraftEdits(entries) {
      const view = currentProjectView();
      if (!view.active()) throw new Error("请先打开项目");
      entries ||= [...document.querySelectorAll(".draft-section textarea")].map(area => ({
        area, section: area.closest(".draft-section"),
      }));
      const edits = entries.map(({ area, section }) => {
        const sid = section.id.slice("draft-sec-".length);
        const draft = sectionEdits.get(`${view.id}\0${sid}`);
        const base = draft?.base ?? state.project.sections?.[sid] ?? "";
        if (draft) draft.content = area.value;
        return { area, section, sid, draft, base, content: area.value };
      }).filter(edit => edit.content !== edit.base || (edit.draft && edit.draft.remote !== edit.base));
      if (!edits.length) return;
      for (const { draft } of edits) if (draft) draft.pending++;
      try {
        const latest = await api(`/api/projects/${view.id}`);
        if (!view.active()) throw new Error("项目已切换，请重新操作");
        if (latest.material_revision < state.project.material_revision) throw new Error("项目又有更新，请重试保存");
        for (const { sid, draft, base, content } of edits) {
          if (!latest.outline?.some(s => s.id === sid)) throw new Error("章节已在其他页面变化，请先核对目录");
          const remote = latest.sections?.[sid] || "";
          if (remote !== base && remote !== content) {
            state.project.sections ||= {};
            state.project.sections[sid] = remote;
            state.project.material_revision = latest.material_revision;
            if (draft) {
              draft.remote = remote;
              draft.display?.paint();
            }
            throw new Error("章节已在其他页面变化，未覆盖您的编辑，请先核对版本");
          }
        }
        let revision = latest.material_revision;
        for (const { area, section, sid, draft, base, content } of edits) {
          if (!view.active()) throw new Error("项目已切换，请重新操作");
          if ((latest.sections?.[sid] || "") !== content) {
            revision = await writeSection(view.id, sid, base, content, revision);
          }
          if (state.project?.id === view.id && state.project.material_revision > revision &&
              state.project.sections?.[sid] !== content) throw new Error("已有更新的版本，请核对章节后继续");
          if (draft) {
            draft.content = draft.display?.editor.value ?? draft.content;
            draft.base = content;
            draft.remote = content;
            draft.display?.paint();
          }
          const reopened = draft && state.project?.id === view.id &&
            sectionEdits.get(`${view.id}\0${sid}`) === draft;
          if (view.active() || reopened) {
            state.project.sections ||= {};
            state.project.sections[sid] = content;
            if (revision !== undefined) state.project.material_revision = Math.max(revision, state.project.material_revision ?? 0);
          }
          if (!view.active()) throw new Error("项目已切换，请在当前项目重新操作");
          const changedWhileSaving = (draft?.content ?? area.value) !== content;
          if (!draft) {
            const statusPill = section.querySelector(".section-status-pill");
            if (statusPill) setDraftSaveStatus(statusPill, !changedWhileSaving);
          }
          updateDraftStats();
          if (changedWhileSaving) throw new Error("保存期间正文又有修改，请保存最新版本后再继续");
        }
      } finally {
        for (const { draft } of edits) if (draft) draft.pending--;
      }
    }

    return {
      open, detachEditors, removeProject, flushDraftEdits, confirmGeneratedSection, applyGeneratedSection,
      clear: () => sectionEdits.clear(),
      isAttached: (key, editor) => sectionEdits.get(key)?.display?.editor === editor,
      hasUnsaved: () => [...sectionEdits.values()].some(draft => draft.content !== draft.base || draft.pending),
    };
  }
  window.SectionEdits = { create };
})();
