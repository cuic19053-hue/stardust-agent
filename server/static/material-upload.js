(() => {
  "use strict";

  function create({ state, $, api }) {
    function uploadLimitMB() {
      const bytes = Number(state.authConfig?.max_upload_bytes);
      return Math.floor((bytes > 0 ? Math.min(bytes, 50_000_000) : 50_000_000) / 1_000_000);
    }

    function oversizedUploadError(files) {
      const max = uploadLimitMB();
      const oversized = Array.from(files).filter(file => file.size > max * 1_000_000);
      return oversized.length
        ? `${oversized.map(file => `“${file.name}”`).join("、")}超过上传上限：单个文件最大 ${max} MB，请压缩或拆分后重试。`
        : "";
    }

    async function uploadFilesToCloud(fileList, kind, projectId = state.project.id, isLeader = false) {
      if (!state.authClient || !state.authSession?.user?.id) {
        throw new Error("登录状态已失效，请重新登录后上传。");
      }
      const mimeByExtension = {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
      };
      const documents = [];
      for (const file of fileList) {
        const extension = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
        const contentType = file.type || mimeByExtension[extension] || "application/octet-stream";
        const max = Math.min(state.authConfig?.max_upload_bytes || 50_000_000, 50_000_000);
        if (!mimeByExtension[extension] || (kind === "photo" && !extension.match(/^\.(png|jpe?g|webp)$/))) {
          documents.push({ error: "暂不支持该文件格式。", document: { original_name: file.name } });
          continue;
        }
        if (!file.size || file.size > max) {
          documents.push({ error: `文件为空或超过 ${Math.floor(max / 1_000_000)} MB。`, document: { original_name: file.name } });
          continue;
        }
        let intent;
        let saved;
        try {
          const endpoint = state.authConfig?.storage_upload_endpoint;
          if (endpoint) {
            intent = await cloudUploadIntent(file, isLeader ? "leader-photo" : kind, projectId);
            if (!intent.uploaded) {
              await uploadResumable(file, intent, contentType, endpoint);
              intent.uploaded = true;
              try { localStorage.setItem(intent.key, JSON.stringify(intent)); } catch {}
            }
          } else {
            if (file.size > 4_000_000) throw new Error("大文件通道尚未启用，请联系管理员完成 Cloudflare 配置；不要反复重传。");
            const documentId = crypto.randomUUID().replaceAll("-", "");
            intent = { documentId, storagePath: `${state.authSession.user.id}/${projectId}/${documentId}${extension}` };
            const { error } = await state.authClient.storage.from("stardust-project-files")
              .upload(intent.storagePath, file, { contentType, upsert: false });
            if (error) throw new Error(error.message);
          }
          $("#uploadResult").textContent = `${file.name}：原文件已上传，正在登记…`;
          const registered = await api(`/api/projects/${projectId}/documents/from-storage`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              document_id: intent.documentId,
              original_name: file.name,
              storage_path: intent.storagePath,
              kind,
              content_type: contentType,
              process_now: false,
              is_leader: isLeader,
            }),
          });
          saved = registered.documents?.[0]?.document;
          if (!saved?.id) throw new Error("未收到材料登记结果。");
          // An acknowledged material survives parsing errors/timeouts and can be retried by ID.
          if (intent.key) { try { localStorage.removeItem(intent.key); } catch {} }
          $("#uploadResult").textContent = `${file.name}：原文件已保存，正在提取文字…`;
          const processed = await api(`/api/documents/${saved.id}/process`, { method: "POST" });
          documents.push(...(processed.documents || []));
        } catch (error) {
          // A timeout is ambiguous: never delete a successfully uploaded object.
          // Retain the intent until registration is acknowledged, making reselection idempotent.
          documents.push({
            error: saved ? `原文件已保存，识别未完成，可在材料卡片重试。${error.message}` : error.message,
            document: saved || { original_name: file.name },
          });
        }
      }
      return { documents };
    }

    async function cloudUploadIntent(file, kind, projectId) {
      const uid = state.authSession.user.id;
      // A stable fingerprint scoped to user + project + kind enables reselection after interruption.
      // Sample both ends to avoid accidentally resuming another file with identical name/size.
      const sample = await new Blob([file.slice(0, 65536), file.slice(Math.max(0, file.size - 65536))]).arrayBuffer();
      const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", sample))]
        .map(b => b.toString(16).padStart(2, "0")).join("");
      const key = `stardust-upload-v1:${uid}:${projectId}:${kind}:${encodeURIComponent(file.name)}:${file.size}:${file.lastModified}:${digest}`;
      try {
        const previous = JSON.parse(localStorage.getItem(key) || "null");
        if (previous && /^[a-f0-9]{32}$/.test(previous.documentId)
            && previous.storagePath === `${uid}/${projectId}/${previous.documentId}${file.name.slice(file.name.lastIndexOf(".")).toLowerCase()}`
            && Date.now() - previous.createdAt < 23 * 3600_000) return { ...previous, key };
      } catch {}
      const documentId = crypto.randomUUID().replaceAll("-", "");
      const intent = { key, documentId,
        storagePath: `${uid}/${projectId}/${documentId}${file.name.slice(file.name.lastIndexOf(".")).toLowerCase()}`,
        createdAt: Date.now(), uploaded: false };
      try { localStorage.setItem(key, JSON.stringify(intent)); } catch {}
      return intent;
    }

    async function uploadResumable(file, intent, contentType, endpoint) {
      if (!window.tus?.Upload) throw new Error("断点续传组件未加载，请刷新网页后重试。");
      const expectedUser = state.authSession.user.id;
      // Only accept our own dedicated route; never send a JWT to a supplied external URL.
      if (endpoint !== "/uploads/resumable") throw new Error("上传通道配置无效。");
      const health = await fetch("/uploads/health", { cache: "no-store" });
      if (!health.ok || !(health.headers.get("content-type") || "").includes("application/json"))
        throw new Error("大文件通道暂不可用，请联系管理员检查 Worker 路由。");
      await new Promise((resolve, reject) => {
        const uploader = new window.tus.Upload(file, {
          endpoint,
          chunkSize: 6 * 1024 * 1024,
          retryDelays: [0, 1000, 3000, 5000, 10000],
          uploadDataDuringCreation: false,
          removeFingerprintOnSuccess: true,
          fingerprint: async () => intent.key,
          metadata: { bucketName: "stardust-project-files", objectName: intent.storagePath, contentType, cacheControl: "0" },
          onBeforeRequest: async (request) => {
            const url = new URL(request.getURL(), window.location.origin);
            if (url.origin !== window.location.origin || !/^\/uploads\/resumable(?:\/[^/]+)?$/.test(url.pathname))
              throw new Error("上传续传地址无效。");
            const { data, error } = await state.authClient.auth.getSession();
            if (error || !data.session?.access_token || data.session.user.id !== expectedUser
                || state.authSession?.user?.id !== expectedUser) throw new Error("登录已失效，请重新登录后继续上传。");
            request.setHeader("Authorization", `Bearer ${data.session.access_token}`);
          },
          onProgress: (sent, total) => {
            $("#uploadResult").textContent = `${file.name}：正在上传 ${Math.floor(sent / total * 100)}%（断网后可重新选择同一文件续传）`;
          },
          onError: (error) => {
            const status = error.originalResponse?.getStatus();
            reject(new Error(status === 413 ? "文件超过云端存储桶允许的大小，请联系管理员。"
              : status === 401 ? "登录已过期，请重新登录后选择同一文件继续上传。"
              : "文件上传中断，请重新选择同一文件续传；如持续失败，请联系管理员。"));
          },
          onSuccess: resolve,
        });
        // Blocked localStorage must not prevent a new upload; it only disables cross-refresh resume.
        uploader.findPreviousUploads().catch(() => []).then(previous => {
          const valid = previous.find(item => {
            try {
              const url = new URL(item.uploadUrl, window.location.origin);
              return url.origin === window.location.origin && url.pathname.startsWith("/uploads/resumable/");
            } catch { return false; }
          });
          if (valid) uploader.resumeFromPreviousUpload(valid);
          uploader.start();
        }).catch(reject);
      });
    }

    return { uploadLimitMB, oversizedUploadError, uploadFilesToCloud, cloudUploadIntent, uploadResumable };
  }

  window.MaterialUpload = { create };
})();
