(() => {
  "use strict";

  const wordType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

  class WordDownloadError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.status = status;
    }
  }

  async function fetchWordFile(url, { token, expectedDigest, controller = new AbortController() } = {}) {
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 45000);
    try {
      const response = await fetch(url, {
        headers, signal: controller.signal, cache: "no-store", redirect: "error",
      });
      if (!response.ok) {
        const { detail } = await response.json().catch(() => ({}));
        throw new WordDownloadError(typeof detail === "string" ? detail : "Word 文件下载失败，请检查登录状态", response.status);
      }
      const digest = response.headers?.get("Repr-Digest");
      if (expectedDigest && digest && digest !== expectedDigest)
        throw new WordDownloadError("云端已有更新的导出版本，请重新导出当前项目。", 409);
      const blob = await response.blob();
      if (response.status === 206 || blob.type.split(";")[0] !== wordType || blob.size < 22)
        throw new WordDownloadError("收到的文件格式有误或内容不完整。");
      const signature = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
      if (signature[0] !== 0x50 || signature[1] !== 0x4b || signature[2] !== 3 || signature[3] !== 4)
        throw new WordDownloadError("收到的文件缺少 Word 文档结构。");
      const knownDigest = expectedDigest || digest;
      if (knownDigest && globalThis.crypto?.subtle) {
        const hash = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
        const actual = btoa(String.fromCharCode(...new Uint8Array(hash)));
        if (knownDigest !== `sha-256=:${actual}:`) throw new WordDownloadError("Word 文件校验失败，请重试下载。");
      }
      return blob;
    } catch (error) {
      if (error instanceof WordDownloadError) throw error;
      throw new WordDownloadError(timedOut ? "Word 下载超时。" : "Word 下载中断，请检查网络。");
    } finally {
      clearTimeout(timer);
    }
  }

  function create({ state, $, api, chatProjectIsActive, recoveryOwner, flushDraftEdits, showChatDrafts, toast, openRechargeDialog }) {
    const recovery = window.DraftRecovery.create("stardust-word-export-v1:", recoveryOwner);
    const attempts = new Map();
    let exportDownloadUrl = null;
    let exportInProgress = false;
    let exportArtifactRevision = 0;
    let pendingDownload = null;
    let downloadController = null;

    function exportButtons() {
      return [$("#exportBtn"), $("#quickExportTopBtn"), $("#headingExportBtn")];
    }

    function attemptFor(projectId) {
      if (!projectId) return null;
      const saved = attempts.get(projectId);
      const stored = saved?.owner === recoveryOwner() ? saved?.entry : recovery.read(projectId);
      const entry = typeof stored === "string" ? { kind: "classic", id: stored } : stored;
      if (typeof entry?.id !== "string" || !/^[a-f0-9]{32}$/.test(entry.id)
          || !["classic", "worker-request", "worker-job"].includes(entry.kind)) return null;
      return entry;
    }

    function rememberAttempt(projectId, entry) {
      if (entry) attempts.set(projectId, { entry, owner: recoveryOwner() });
      else attempts.delete(projectId);
      recovery.write(projectId, entry);
    }

    function clearRecovery(projectId) {
      if (projectId) attempts.delete(projectId);
      else attempts.clear();
      recovery.clear(projectId ? `${projectId}\0` : "");
    }

    function syncButtons() {
      if (exportInProgress) return;
      const projectId = state.project?.id;
      const retry = pendingDownload && pendingDownload.projectId === projectId;
      const label = retry ? "重试下载 Word" : attemptFor(projectId) ? "检查 Word 导出结果" : "";
      const [btn, topBtn, headingBtn] = exportButtons();
      for (const button of [btn, topBtn, headingBtn]) {
        if (!button) continue;
        button.disabled = false;
        if (label) button.textContent = label;
      }
      if (!label) {
        if (btn) btn.innerHTML = '生成并下载 Word 申报书 <span>↓</span>';
        if (topBtn) topBtn.innerHTML = '<span>📥 导出草稿</span>';
        if (headingBtn) headingBtn.textContent = "📥 导出 Word";
      }
    }

    function invalidate({ preserveRecovery = false } = {}) {
      exportArtifactRevision++;
      const projectId = state.project?.id;
      const hadAttempt = attemptFor(projectId);
      if (hadAttempt && !preserveRecovery) rememberAttempt(projectId, null);
      const hadDownload = pendingDownload;
      pendingDownload = null;
      if (hadAttempt || hadDownload) syncButtons();
      downloadController?.abort();
      if (exportDownloadUrl) URL.revokeObjectURL(exportDownloadUrl);
      exportDownloadUrl = null;
      $("#exportResult")?.replaceChildren();
    }

    async function fetchExportBlob(url, projectId, options) {
      if (url !== `/api/projects/${encodeURIComponent(projectId)}/download`)
        throw new Error("下载地址不属于当前项目");
      return fetchWordFile(url, { token: state.authSession?.access_token, ...options });
    }

    async function downloadCurrent(isCurrent) {
      const { url, projectId, worker, digest, warnings } = pendingDownload;
      downloadController = new AbortController();
      const options = { expectedDigest: digest, controller: downloadController };
      const blob = worker
        ? await fetchWordFile(url, { ...options, token: state.authSession?.access_token })
        : await fetchExportBlob(url, projectId, options);
      if (!isCurrent()) return;
      pendingDownload = null;
      rememberAttempt(projectId, null);
      if (exportDownloadUrl) URL.revokeObjectURL(exportDownloadUrl);
      exportDownloadUrl = URL.createObjectURL(blob);

      const link = document.createElement("a");
      link.href = exportDownloadUrl;
      link.textContent = "下载 Word 申报书";
      link.download = `${state.project?.state?.skill_name || "申报材料"}草稿.docx`;
      const text = document.createElement("span");
      text.textContent = warnings?.length ? `已生成草稿。${warnings.join(" ")}` : "Word 草稿已生成。";
      $("#exportResult").replaceChildren(text, link);

      // The same verified file remains available from the manual link.
      const autoDownload = document.createElement("a");
      autoDownload.href = link.href;
      autoDownload.download = link.download;
      document.body.appendChild(autoDownload);
      autoDownload.click();
      document.body.removeChild(autoDownload);
      toast("Word 草稿已生成并开始下载。");
    }

    async function requestExport(url, options, timeout) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);
      try {
        return await api(url, { ...options, signal: controller.signal, cache: "no-store" });
      } catch (error) {
        if (controller.signal.aborted || error instanceof TypeError)
          throw new Error("暂未收到 Word 导出结果。");
        throw error;
      } finally {
        clearTimeout(timer);
      }
    }

    async function acceptResult(result, projectId, isCurrent) {
      if (!isCurrent()) return;
      const notices = {
        processing: "Word 草稿仍在生成，请稍后再检查。",
        stale: "项目内容已更新，请重新导出 Word 草稿。",
        failed: "这次 Word 导出失败，可以重新生成。",
        expired: "这次 Word 导出已超时，可以重新生成。",
        missing: "这次导出暂未找到，可以重新生成。",
      };
      if (Object.hasOwn(notices, result?.status)) {
        if (result.status !== "processing") rememberAttempt(projectId, null);
        toast(notices[result.status], result.status !== "processing");
        return;
      }
      if (result?.download_url !== `/api/projects/${encodeURIComponent(projectId)}/download`)
        throw new Error("下载地址不属于当前项目");
      if (result.status === "completed" && !/^sha-256=:[A-Za-z0-9+/]{43}=:$/.test(result.download_digest))
        throw new Error("云端暂未返回可核对的 Word 文件版本。");
      pendingDownload = { projectId, url: result.download_url,
        digest: result.download_digest, warnings: result.warnings };
      await downloadCurrent(isCurrent);
    }

    async function downloadWorkerResult(url, warnings, projectId, isCurrent) {
      if (!isCurrent()) return;
      const prefix = `/api/projects/${encodeURIComponent(projectId)}/material/jobs/`;
      if (!url.startsWith(prefix) || !/^[a-f0-9]{32}\/files\/proposal\.docx$/.test(url.slice(prefix.length)))
        throw new Error("下载地址不属于当前项目");
      pendingDownload = { projectId, url: url + "?current=true", worker: true, warnings };
      await downloadCurrent(isCurrent);
    }

    async function checkWorkerExport(attempt, projectId, isCurrent) {
      const base = `/api/projects/${encodeURIComponent(projectId)}/material`;
      const path = attempt.kind === "worker-job" ? "/jobs/" : "/exports/by-request/";
      const job = await requestExport(base + path + attempt.id, {}, 15000);
      if (!isCurrent()) return;
      if (job?.kind !== "export" || job.project_id !== projectId || !/^[a-f0-9]{32}$/.test(job.id)
          || (attempt.kind === "worker-job" && job.id !== attempt.id))
        throw new Error("暂时无法确认这次后台导出的任务。");
      if (["pending", "running"].includes(job.status)) {
        rememberAttempt(projectId, { kind: "worker-job", id: job.id });
        toast("Word 草稿仍在后台生成，请稍后再检查。");
      } else if (job.status === "completed") {
        rememberAttempt(projectId, { kind: "worker-job", id: job.id });
        await downloadWorkerResult(`${base}/jobs/${job.id}/files/proposal.docx`, job.result?.report?.warnings,
          projectId, isCurrent);
      } else if (["failed", "cancelled", "needs_review"].includes(job.status)) {
        rememberAttempt(projectId, null);
        toast(job.error || "这次后台导出未完成，可以重新生成。", true);
      } else {
        throw new Error("暂时无法确认后台导出的状态。");
      }
    }

    async function exportDoc(options = {}) {
      if (exportInProgress || !state.project) return;
      exportInProgress = true;
      const projectId = state.project.id;
      const artifactRevision = exportArtifactRevision;
      const isCurrentExport = () => chatProjectIsActive(projectId) && artifactRevision === exportArtifactRevision;
      const retry = pendingDownload?.projectId === projectId;
      const attempt = attemptFor(projectId);
      const endpoint = `/api/projects/${encodeURIComponent(projectId)}/export`;
      if (!retry) pendingDownload = null;
      for (const button of exportButtons()) {
        if (!button) continue;
        button.disabled = true;
        button.textContent = retry ? "正在下载…" : attempt ? "正在检查…" : "正在生成…";
      }
      try {
        if (retry) {
          await downloadCurrent(isCurrentExport);
          return;
        }
        await flushDraftEdits();
        if (!isCurrentExport()) return;
        if (attempt?.kind === "classic") {
          const result = await requestExport(`${endpoint}/status/${attempt.id}`, {}, 15000);
          await acceptResult(result, projectId, isCurrentExport);
          return;
        }
        if (attempt) {
          await checkWorkerExport(attempt, projectId, isCurrentExport);
          return;
        }
        if (!options.skipChatDrafts) {
          const preview = await api(`/api/projects/${projectId}/chat-drafts`);
          if (!isCurrentExport()) return;
          if (preview.items.some(item => !item.already_saved)) {
            await showChatDrafts(preview, exportDoc);
            return;
          }
        }
        const hasContent = (state.project.outline || state.outline || [])
          .some(s => (state.project.sections?.[s.id] || "").trim());
        let allowEmpty = false;
        if (!hasContent) {
          allowEmpty = confirm("尚无已保存的正文。是否仅导出空白/待补充草稿？这不会包含聊天里未保存的内容。");
          if (!allowEmpty) return;
        }
        const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)),
          byte => byte.toString(16).padStart(2, "0")).join("");
        if (window.MaterialPilot?.active()) {
          await window.MaterialPilot.exportDocument({ allow_empty_draft: allowEmpty, idempotency_key: requestId },
            isCurrentExport,
            (url, warnings) => downloadWorkerResult(url, warnings, projectId, isCurrentExport),
            entry => { if (isCurrentExport()) rememberAttempt(projectId, entry); });
          return;
        }
        rememberAttempt(projectId, { kind: "classic", id: requestId });
        const result = await requestExport(endpoint, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ allow_empty_draft: allowEmpty, request_id: requestId }),
        }, 65000);
        await acceptResult(result, projectId, isCurrentExport);
      } catch (error) {
        if (!isCurrentExport()) return;
        if ([400, 404, 409, 422].includes(error.status)) {
          rememberAttempt(projectId, null);
          pendingDownload = null;
        }
        const action = pendingDownload ? "重试下载 Word" : attemptFor(projectId) ? "检查 Word 导出结果" : "";
        toast(action ? `${error.message} 可点击「${action}」。` : error.message, true);
        if (!pendingDownload && ((error.message || "").includes("积分不足") || (error.message || "").includes("402"))) {
          openRechargeDialog();
        }
      } finally {
        downloadController = null;
        exportInProgress = false;
        syncButtons();
      }
    }

    return { exportDoc, fetchExportBlob, invalidate, syncButtons, clearRecovery };
  }

  window.WordExport = { create, fetchWordFile };
})();
