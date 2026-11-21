(() => {
  "use strict";

  function create({ state, $, api, chatProjectIsActive, flushDraftEdits, showChatDrafts, toast, openRechargeDialog }) {
    let exportDownloadUrl = null;
    let exportInProgress = false;
    let exportArtifactRevision = 0;

    function invalidate() {
      exportArtifactRevision++;
      if (exportDownloadUrl) URL.revokeObjectURL(exportDownloadUrl);
      exportDownloadUrl = null;
      $("#exportResult")?.replaceChildren();
    }

    async function fetchExportBlob(url, projectId) {
      if (url !== `/api/projects/${encodeURIComponent(projectId)}/download`)
        throw new Error("下载地址不属于当前项目");
      const headers = {};
      if (state.authSession?.access_token) headers.Authorization = `Bearer ${state.authSession.access_token}`;
      const response = await fetch(url, { headers });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || "Word 文件下载失败，请检查登录状态");
      }
      return response.blob();
    }

    async function exportDoc(options = {}) {
      if (exportInProgress || !state.project) return;
      exportInProgress = true;
      const projectId = state.project.id;
      const artifactRevision = exportArtifactRevision;
      const isCurrentExport = () => chatProjectIsActive(projectId) && artifactRevision === exportArtifactRevision;
      const btn = $("#exportBtn");
      const topBtn = $("#quickExportTopBtn");
      const headingBtn = $("#headingExportBtn");
      [btn, topBtn, headingBtn].forEach(b => {
        if (b) {
          b.disabled = true;
          b.dataset.origText = b.textContent;
          b.textContent = "正在生成…";
        }
      });
      try {
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
          await window.MaterialPilot.exportDocument({ allow_empty_draft: allowEmpty }, isCurrentExport);
          return;
        }
        const result = await api(`/api/projects/${projectId}/export`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ allow_empty_draft: allowEmpty }),
        });
        if (!isCurrentExport()) return;
        const blob = await fetchExportBlob(result.download_url, projectId);
        if (!isCurrentExport()) return;
        if (exportDownloadUrl) URL.revokeObjectURL(exportDownloadUrl);
        exportDownloadUrl = URL.createObjectURL(blob);
        $("#exportResult").replaceChildren();
        const text = document.createElement("span");
        text.textContent = result.warnings?.length ? `已生成草稿。${result.warnings.join(" ")}` : "Word 草稿已生成。";
        const link = document.createElement("a");
        link.href = exportDownloadUrl;
        link.textContent = "下载 Word 申报书";
        link.download = `${state.project?.state?.skill_name || "申报材料"}草稿.docx`;
        $("#exportResult").append(text, link);

        // 自动触发浏览器安全下载
        const autoDownload = document.createElement("a");
        autoDownload.href = exportDownloadUrl;
        autoDownload.download = link.download;
        document.body.appendChild(autoDownload);
        autoDownload.click();
        document.body.removeChild(autoDownload);

        toast("Word 草稿已生成并开始下载！");
      } catch (error) {
        toast(error.message, true);
        if ((error.message || "").includes("积分不足") || (error.message || "").includes("402")) {
          openRechargeDialog();
        }
      } finally {
        exportInProgress = false;
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '生成并下载 Word 申报书 <span>↓</span>';
        }
        if (topBtn) {
          topBtn.disabled = false;
          topBtn.innerHTML = '<span>📥 导出草稿</span>';
        }
        if (headingBtn) {
          headingBtn.disabled = false;
          headingBtn.textContent = '📥 导出 Word';
        }
      }
    }

    return { exportDoc, fetchExportBlob, invalidate };
  }

  window.WordExport = { create };
})();
