(() => {
  "use strict";

  const normalize = value => String(value ?? "").replace(/\r\n?/g, "\n").trim();
  const renamesProject = (entry, value = entry.value) => entry.group === "facts" && entry.key === "project_name" && Boolean(value);
  function readValue(project, group, key) {
    const fields = group === "facts" ? project.state?.confirmed_facts : project;
    return normalize(fields && Object.hasOwn(fields, key) ? fields[key] : "");
  }

  function create(recoveryOwner, toast) {
    const recovery = window.DraftRecovery.create("stardust-facts-recovery-v1:", recoveryOwner);
    const drafts = new Map();
    let warned = false;

    function tracked(id, draft) {
      return drafts.get(id) === draft && draft.owner === recoveryOwner();
    }

    function persist(id, draft) {
      if (!tracked(id, draft)) return;
      const entries = [...draft.entries.values()];
      if (recovery.write(id, entries.length ? entries : null) || !entries.length || warned) return;
      warned = true;
      toast("浏览器暂时无法保留事实草稿，请先保存再刷新。", true);
    }

    function fieldId(group, key) {
      return `${group}:${key}`;
    }

    function open(project, reconcileWhileSaving = false) {
      const id = project.id;
      const owner = recoveryOwner();
      if (drafts.get(id)?.owner !== owner || !drafts.has(id)) {
        const saved = recovery.read(id);
        const draft = { owner, entries: new Map(), recovered: false, saving: false };
        for (const entry of Array.isArray(saved) ? saved : []) {
          if (["facts", "project"].includes(entry?.group) && /^[a-z][a-z0-9_]{1,63}$/.test(entry.key)
              && typeof entry.base === "string" && typeof entry.value === "string") {
            draft.entries.set(fieldId(entry.group, entry.key), {
              group: entry.group, key: entry.key, base: entry.base, value: entry.value,
              baseTitle: typeof entry.baseTitle === "string" ? entry.baseTitle : normalize(project.title),
              submitted: typeof entry.submitted === "string" ? entry.submitted : undefined,
            });
          }
        }
        draft.recovered = draft.entries.size > 0;
        drafts.set(id, draft);
      }
      const draft = drafts.get(id);
      if (draft.saving && !reconcileWhileSaving) return draft;
      for (const [field, entry] of draft.entries) {
        const remote = readValue(project, entry.group, entry.key);
        if (entry.submitted === remote && (!renamesProject(entry, remote) || normalize(project.title) === remote)) {
          entry.base = remote;
          entry.baseTitle = normalize(project.title);
          entry.submitted = undefined;
        }
        if (entry.value === remote && entry.submitted === undefined) draft.entries.delete(field);
      }
      persist(id, draft);
      return draft;
    }

    function edit(project, group, key, text) {
      const draft = drafts.get(project.id) || open(project);
      if (!tracked(project.id, draft)) return;
      const id = fieldId(group, key);
      const remote = readValue(project, group, key);
      const entry = draft.entries.get(id) || { group, key, base: remote, baseTitle: normalize(project.title) };
      const newValue = normalize(text);
      if (newValue === (entry.value ?? remote)) return;
      draft.recovered = false;
      entry.value = newValue;
      draft.entries.set(id, entry);
      if (entry.value === entry.base && entry.value === remote && entry.submitted === undefined)
        draft.entries.delete(id);
      persist(project.id, draft);
    }

    function value(project, draft, group, key) {
      return draft.entries.get(fieldId(group, key))?.value ?? readValue(project, group, key);
    }

    function conflicts(project, draft) {
      return [...draft.entries.values()].filter(entry => {
        const remote = readValue(project, entry.group, entry.key);
        if (entry.value === remote && entry.submitted === undefined) return false;
        return remote !== entry.base || (renamesProject(entry) && normalize(project.title) !== entry.baseTitle);
      });
    }

    function resolve(project, entry, keep) {
      const draft = drafts.get(project.id);
      if (!draft || !tracked(project.id, draft) || draft.saving) return;
      const id = fieldId(entry.group, entry.key);
      if (draft.entries.get(id) !== entry) return;
      entry.base = readValue(project, entry.group, entry.key);
      entry.baseTitle = normalize(project.title);
      entry.submitted = undefined;
      if (!keep || entry.base === entry.value) draft.entries.delete(id);
      persist(project.id, draft);
    }

    function prepare(project, draft) {
      const changes = [...draft.entries.values()].filter(entry => entry.value !== readValue(project, entry.group, entry.key)
        || entry.submitted !== undefined).map(entry => ({ entry, value: entry.value, previous: entry.submitted }));
      const payload = { facts: {}, project: {}, expected_revision: project.material_revision,
        base: { facts: {}, project: {} } };
      for (const { entry, value, previous } of changes) {
        payload[entry.group][entry.key] = value;
        payload.base[entry.group][entry.key] = entry.base;
        if (renamesProject(entry, value)) payload.base.project.title = entry.baseTitle;
        if (previous !== undefined && value === readValue(project, entry.group, entry.key))
          payload.supersedes_pending_save = true;
      }
      return { changes, payload };
    }

    function start(id, draft, changes) {
      for (const { entry, value } of changes) entry.submitted = value;
      persist(id, draft);
    }

    function finish(id, draft, changes, rejected = false) {
      if (!tracked(id, draft)) return;
      for (const { entry, value, previous } of changes) {
        if (rejected) {
          entry.submitted = previous;
          continue;
        }
        entry.base = value;
        if (renamesProject(entry, value)) entry.baseTitle = value;
        entry.submitted = undefined;
        if (entry.value === value) draft.entries.delete(fieldId(entry.group, entry.key));
      }
      persist(id, draft);
    }

    function removeProject(id) {
      drafts.delete(id);
      recovery.clear(`${id}\0`);
    }

    function clear() {
      drafts.clear();
      warned = false;
      recovery.clear();
    }

    function hasUnsaved() {
      const owner = recoveryOwner();
      return [...drafts.values()].some(draft => draft.owner === owner && draft.entries.size) || recovery.hasEntries();
    }

    return { open, tracked, edit, value, readValue, conflicts, resolve, prepare, start, finish, removeProject, clear, hasUnsaved };
  }

  window.FactEdits = { create };
})();
