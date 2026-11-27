(() => {
  "use strict";

  const recoveryPrefix = "stardust-section-recovery-v1:";
  const needsRecovery = draft => draft.content !== draft.base || draft.remote !== draft.base || draft.submitted !== undefined;

  function create({ state, api, currentProjectView, recoveryOwner, setDraftSaveStatus, updateDraftStats, toast }) {
    const sectionEdits = new Map();
    let notifiedStorageFailure = false;
    const storageKey = (key, owner) => `${recoveryPrefix}${key}\0${owner}`;

    function readRecovery(key, owner) {
      if (!owner) return null;
      try {
        const raw = sessionStorage.getItem(storageKey(key, owner));
        if (!raw) return null;
        const saved = JSON.parse(raw);
        return typeof saved?.base === "string" && typeof saved.content === "string"
          ? { owner, base: saved.base, content: saved.content,
            submitted: typeof saved.submitted === "string" ? saved.submitted : undefined, recovered: true, pending: 0 }
          : null;
      } catch {
        return null;
      }
    }

    function persistEdit(key, draft) {
      if (!draft.owner || draft.owner !== recoveryOwner() || sectionEdits.get(key) !== draft) return;
      const keyInStorage = storageKey(key, draft.owner);
      const dirty = needsRecovery(draft);
      try {
        if (dirty) {
          const { base, content, submitted } = draft;
          sessionStorage.setItem(keyInStorage, JSON.stringify({ base, content, submitted }));
        } else {
          sessionStorage.removeItem(keyInStorage);
        }
      } catch {
        if (!dirty || notifiedStorageFailure) return;
        notifiedStorageFailure = true;
        toast("浏览器暂时无法保留章节草稿，请先保存正文再刷新。", true);
      }
    }

    function removeRecovery(prefix) {
      try {
        for (let i = sessionStorage.length - 1; i >= 0; i--) {
          const key = sessionStorage.key(i);
          if (key?.startsWith(prefix)) sessionStorage.removeItem(key);
        }
      } catch {
        // Browsers can deny access to session storage.
      }
    }

    function setRemote(draft, content) {
      draft.remote = content;
      if (content === draft.submitted) {
        draft.base = content;
        draft.submitted = undefined;
      }
      if (content === draft.content) {
        draft.base = content;
        draft.recovered = false;
      }
    }

    function open(key, remoteContent) {
      const owner = recoveryOwner();
      let draft = sectionEdits.get(key);
      if (!draft || draft.owner !== owner) {
        draft = readRecovery(key, owner) || { owner, base: remoteContent, content: remoteContent, pending: 0 };
        sectionEdits.set(key, draft);
      }
      setRemote(draft, remoteContent);
      persistEdit(key, draft);
      return draft;
    }

    function record(key, content, resolveConflict = false) {
      const draft = sectionEdits.get(key);
      if (!draft || draft.owner !== recoveryOwner()) return;
      if (resolveConflict) {
        draft.base = draft.remote;
        if (!draft.pending) draft.submitted = undefined;
      }
      draft.content = content;
      draft.recovered = false;
      if (content === draft.remote) draft.base = draft.remote;
      persistEdit(key, draft);
    }

    function detachEditors() {
      for (const [key, draft] of sectionEdits) {
        draft.display = null;
        if (!draft.pending && !needsRecovery(draft)) sectionEdits.delete(key);
      }
    }

    function removeProject(projectId) {
      const prefix = `${projectId}\0`;
      for (const key of sectionEdits.keys()) {
        if (key.startsWith(prefix)) sectionEdits.delete(key);
      }
      removeRecovery(recoveryPrefix + prefix);
    }

    function clear() {
      sectionEdits.clear();
      notifiedStorageFailure = false;
      removeRecovery(recoveryPrefix);
    }

    function hasUnsaved() {
      const owner = recoveryOwner();
      if ([...sectionEdits.values()].some(draft => draft.owner === owner && (draft.pending || needsRecovery(draft)))) return true;
      if (!owner) return false;
      try {
        for (let i = 0; i < sessionStorage.length; i++) {
          const key = sessionStorage.key(i);
          if (key?.startsWith(recoveryPrefix) && key.endsWith(`\0${owner}`)) return true;
        }
      } catch {}
      return false;
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
      const key = `${state.project.id}\0${sid}`;
      const draft = sectionEdits.get(key);
      if (draft) {
        const current = draft.display?.editor.value ?? draft.content;
        draft.content = current === draft.base ? content : current;
        draft.base = draft.remote = content;
        persistEdit(key, draft);
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
        const key = `${view.id}\0${sid}`;
        const draft = sectionEdits.get(key);
        const base = draft?.base ?? state.project.sections?.[sid] ?? "";
        if (draft && draft.content !== area.value) record(key, area.value);
        return { area, section, key, sid, draft, base, content: area.value };
      }).filter(({ content, base, draft }) => content !== base ||
        (draft && (draft.remote !== base || draft.submitted !== undefined)));
      if (!edits.length) return;
      for (const { draft } of edits) if (draft) draft.pending++;
      try {
        const latest = await api(`/api/projects/${view.id}`);
        if (!view.active()) throw new Error("项目已切换，请重新操作");
        if (latest.material_revision < state.project.material_revision) throw new Error("项目又有更新，请重试保存");
        for (const edit of edits) {
          const { key, sid, draft, content } = edit;
          if (!latest.outline?.some(s => s.id === sid)) throw new Error("章节已在其他页面变化，请先核对目录");
          const remote = latest.sections?.[sid] || "";
          if (draft && remote === draft.submitted) {
            setRemote(draft, remote);
            edit.base = draft.base;
            persistEdit(key, draft);
          }
          if (remote !== edit.base && remote !== content) {
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
        for (const { area, section, key, sid, draft, base, content } of edits) {
          if (!view.active()) throw new Error("项目已切换，请重新操作");
          // An explicit save of the original text still needs a conditional write when a prior write may land.
          if ((latest.sections?.[sid] || "") !== content || draft?.submitted !== undefined) {
            const previousSubmission = draft?.submitted;
            if (draft) {
              draft.submitted = content;
              persistEdit(key, draft);
            }
            try {
              revision = await writeSection(view.id, sid, base, content, revision);
            } catch (error) {
              if (draft?.submitted === content && error.status >= 400 && error.status < 500) {
                draft.submitted = previousSubmission;
                persistEdit(key, draft);
              }
              throw error;
            }
          }
          if (state.project?.id === view.id && state.project.material_revision > revision &&
              state.project.sections?.[sid] !== content) throw new Error("已有更新的版本，请核对章节后继续");
          if (draft) {
            draft.content = draft.display?.editor.value ?? draft.content;
            draft.base = content;
            draft.remote = content;
            draft.recovered = false;
            if (draft.submitted === content) draft.submitted = undefined;
            persistEdit(key, draft);
            draft.display?.paint();
          }
          const reopened = draft && state.project?.id === view.id &&
            sectionEdits.get(key) === draft;
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
      open, record, detachEditors, removeProject, clear, flushDraftEdits, confirmGeneratedSection, applyGeneratedSection,
      isAttached: (key, editor) => sectionEdits.get(key)?.display?.editor === editor,
      hasUnsaved,
    };
  }
  window.SectionEdits = { create };
})();
