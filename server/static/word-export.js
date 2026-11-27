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

  function create({ state, $, api, chatProjectIsActive, flushDraftEdits, showChatDrafts, toast, openRechargeDialog }) {
    let exportDownloadUrl = null;
    let exportInProgress = false;
    let exportArtifactRevision = 0;
    let pendingDownload = null;
    let downloadController = null;

    function exportButtons() {
      return [$("#exportBtn"), $("#quickExportTopBtn"), $("#headingExportBtn")];
    }

    function showIdleButtons(retry) {
      const [btn, topBtn, headingBtn] = exportButtons();
      for (const button of [btn, topBtn, headingBtn]) {
        if (!button) continue;
        button.disabled = false;
        if (retry) button.textContent = "重试下载 Word";
      }
      if (!retry) {
        if (btn) btn.innerHTML = '生成并下载 Word 申报书 <span>↓</span>';
        if (topBtn) topBtn.innerHTML = '<span>📥 导出草稿</span>';
        if (headingBtn) headingBtn.textContent = "📥 导出 Word";
      }
    }

    function invalidate() {
      exportArtifactRevision++;
      if (pendingDownload && !exportInProgress) showIdleButtons(false);
      pendingDownload = null;
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

    async function exportDoc(options = {}) {
      if (exportInProgress || !state.project) return;
      exportInProgress = true;
      const projectId = state.project.id;
      const artifactRevision = exportArtifactRevision;
      const isCurrentExport = () => chatProjectIsActive(projectId) && artifactRevision === exportArtifactRevision;
      const retry = pendingDownload?.projectId === projectId;
      if (!retry) pendingDownload = null;
      for (const button of exportButtons()) {
        if (!button) continue;
        button.disabled = true;
        button.textContent = retry ? "正在下载…" : "正在生成…";
      }
      try {
        if (retry) {
          await downloadCurrent(isCurrentExport);
          return;
        }
        await flushDraftEdits();
        if (!isCurrentExport()) return;
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
        if (window.MaterialPilot?.active()) {
          await window.MaterialPilot.exportDocument({ allow_empty_draft: allowEmpty }, isCurrentExport, async url => {
            if (!isCurrentExport()) return;
            const workerPrefix = `/api/projects/${encodeURIComponent(projectId)}/material/jobs/`;
            if (!url.startsWith(workerPrefix) || !/^[a-f0-9]{32}\/files\/proposal\.docx$/.test(url.slice(workerPrefix.length)))
              throw new Error("下载地址不属于当前项目");
            pendingDownload = { projectId, url, worker: true };
            await downloadCurrent(isCurrentExport);
          });
          return;
        }
        const result = await api(`/api/projects/${projectId}/export`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ allow_empty_draft: allowEmpty }),
        });
        if (!isCurrentExport()) return;
        if (result.download_url !== `/api/projects/${encodeURIComponent(projectId)}/download`)
          throw new Error("下载地址不属于当前项目");
        pendingDownload = { projectId, url: result.download_url, digest: result.download_digest, warnings: result.warnings };
        await downloadCurrent(isCurrentExport);
      } catch (error) {
        if (!isCurrentExport()) return;
        if (error.status === 404 || error.status === 409) pendingDownload = null;
        const message = pendingDownload ? `${error.message} 可点击「重试下载 Word」。` : error.message;
        toast(message, true);
        if (!pendingDownload && ((error.message || "").includes("积分不足") || (error.message || "").includes("402"))) {
          openRechargeDialog();
        }
      } finally {
        downloadController = null;
        exportInProgress = false;
        showIdleButtons(Boolean(pendingDownload && pendingDownload.projectId === state.project?.id));
      }
    }

    return { exportDoc, fetchExportBlob, invalidate };
  }

  window.WordExport = { create, fetchWordFile };
})();
