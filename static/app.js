(function () {
  "use strict";

  var els = {
    messages: document.getElementById("messages"),
    typing: document.getElementById("typing"),
    typingText: document.getElementById("typing-text"),
    task: document.getElementById("task-input"),
    send: document.getElementById("send-btn"),
    stop: document.getElementById("stop-btn"),
    clear: document.getElementById("clear-btn"),
    reconnect: document.getElementById("reconnect-btn"),
    logout: document.getElementById("logout-btn"),
    fileInput: document.getElementById("file-input"),
    attachments: document.getElementById("attachments"),
    browserBadge: document.getElementById("browser-badge"),
    llmBadge: document.getElementById("llm-badge"),
    wsBadge: document.getElementById("ws-badge"),
    browserState: document.getElementById("browser-state"),
    frame: document.getElementById("vnc-frame"),
    fallback: document.getElementById("vnc-fallback"),
    fileList: document.getElementById("file-list"),
    errorBar: document.getElementById("error-bar")
  };

  var VNC_URL = "/novnc/vnc_lite.html?path=websockify&scale=true";

  var state = {
    ws: null,
    wsAttempts: 0,
    busy: false,
    uploads: [],
    novnc: false,
    maxUploadMb: 10,
    browser: "unknown",
    frameLoaded: false
  };

  // ---------------------------------------------------------------- helpers
  function setBadge(element, kind, label) {
    element.classList.remove("ok", "bad", "warn");
    element.classList.add(kind);
    var span = element.querySelector("span");
    if (span) {
      span.textContent = label;
    }
  }

  function showError(message) {
    els.errorBar.textContent = message;
    els.errorBar.classList.remove("hidden");
    window.clearTimeout(showError._timer);
    showError._timer = window.setTimeout(function () {
      els.errorBar.classList.add("hidden");
    }, 8000);
  }

  function handleAuth(response) {
    if (response.status === 401) {
      window.location.href = "/login";
      return true;
    }
    return false;
  }

  function api(path, options) {
    return fetch(path, options).then(function (response) {
      if (handleAuth(response)) {
        throw new Error("redirecting");
      }
      return response;
    });
  }

  function scrollMessages() {
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function addMessage(kind, text, meta) {
    var wrap = document.createElement("div");
    wrap.className = "message " + kind;

    var bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);

    if (meta) {
      var metaEl = document.createElement("div");
      metaEl.className = "meta";
      metaEl.textContent = meta;
      wrap.appendChild(metaEl);
    }

    els.messages.appendChild(wrap);
    scrollMessages();
    return wrap;
  }

  function setBusy(busy) {
    state.busy = busy;
    els.stop.classList.toggle("hidden", !busy);
    els.send.disabled = busy;
    els.task.disabled = busy;
    if (busy) {
      els.typingText.textContent = "Agent is working...";
      els.typing.classList.remove("hidden");
    } else {
      els.typing.classList.add("hidden");
    }
  }

  // ------------------------------------------------------------- websocket
  function connectWS() {
    var proto = window.location.protocol === "https:" ? "wss" : "ws";
    var ws = new WebSocket(proto + "://" + window.location.host + "/ws");
    state.ws = ws;

    ws.onopen = function () {
      state.wsAttempts = 0;
      setBadge(els.wsBadge, "ok", "Live");
    };

    ws.onclose = function () {
      setBadge(els.wsBadge, "bad", "Offline");
      scheduleReconnect();
    };

    ws.onerror = function () {
      setBadge(els.wsBadge, "warn", "Issues");
    };

    ws.onmessage = function (event) {
      var payload;
      try {
        payload = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      handleEvent(payload);
    };
  }

  function scheduleReconnect() {
    var delay = Math.min(1000 * Math.pow(2, state.wsAttempts), 15000);
    state.wsAttempts += 1;
    window.setTimeout(connectWS, delay);
  }

  function handleEvent(event) {
    var data = event.data || {};
    var message = event.message || "";

    switch (event.type) {
      case "task_started":
        setBusy(true);
        addMessage("assistant", message || "Task started.");
        break;
      case "agent_thinking":
        if (message) {
          els.typingText.textContent = message;
        }
        break;
      case "agent_action":
        setBusy(true);
        addMessage("action", message || "Working...", [data.url, data.title].filter(Boolean).join(" - "));
        break;
      case "task_completed":
        setBusy(false);
        addMessage("assistant", message || "Task completed.");
        break;
      case "task_failed":
        setBusy(false);
        addMessage("error", message || "The task failed.");
        break;
      case "task_stopped":
        setBusy(false);
        addMessage("assistant", message || "Task stopped.");
        break;
      case "task_busy":
        showError(message || "Agent is currently busy.");
        break;
      case "browser_connected":
        state.browser = "connected";
        setBadge(els.browserBadge, "ok", "Browser");
        els.browserState.textContent = "connected";
        break;
      case "browser_disconnected":
        state.browser = "disconnected";
        setBadge(els.browserBadge, "bad", "Browser");
        els.browserState.textContent = "disconnected";
        if (message) {
          showError(message);
        }
        break;
      case "file":
        addFileChip({ name: data.name, url: data.url, size_human: "", source: "download" });
        break;
      case "error":
        addMessage("error", message || "Unexpected error.");
        break;
      case "log":
        if (message) {
          addMessage("action", message);
        }
        break;
      default:
        break;
    }
  }

  // ---------------------------------------------------------------- status
  function refreshStatus() {
    return api("/api/status").then(function (response) {
      return response.json();
    }).then(function (status) {
      state.browser = status.browser;
      state.novnc = !!status.novnc;
      state.maxUploadMb = status.max_upload_mb || 10;

      if (status.browser === "connected") {
        setBadge(els.browserBadge, "ok", "Browser");
      } else {
        setBadge(els.browserBadge, "bad", "Browser");
      }
      els.browserState.textContent = status.browser;

      if (status.llm === "configured") {
        setBadge(els.llmBadge, "ok", "LLM");
      } else {
        setBadge(els.llmBadge, "warn", "LLM missing");
      }

      if (status.novnc) {
        els.fallback.classList.add("hidden");
        if (!state.frameLoaded) {
          els.frame.src = VNC_URL;
          state.frameLoaded = true;
        }
      } else {
        els.frame.classList.add("hidden");
        els.fallback.classList.remove("hidden");
      }
    }).catch(function () {});
  }

  // ----------------------------------------------------------------- files
  function renderAttachments() {
    els.attachments.innerHTML = "";
    if (state.uploads.length === 0) {
      els.attachments.classList.add("hidden");
      return;
    }
    els.attachments.classList.remove("hidden");
    state.uploads.forEach(function (file) {
      var chip = document.createElement("span");
      chip.className = "chip";

      var name = document.createElement("span");
      name.textContent = file.name;
      chip.appendChild(name);

      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-remove";
      remove.textContent = "x";
      remove.title = "Remove attachment";
      remove.addEventListener("click", function () {
        state.uploads = state.uploads.filter(function (item) { return item.id !== file.id; });
        renderAttachments();
      });
      chip.appendChild(remove);
      els.attachments.appendChild(chip);
    });
  }

  function addFileChip(file) {
    if (!els.fileList) {
      return;
    }
    var empty = els.fileList.querySelector(".muted");
    if (empty && els.fileList.children.length === 1) {
      els.fileList.innerHTML = "";
    }
    var link = document.createElement("a");
    link.className = "file-chip";
    link.href = file.url;
    link.title = "Download " + file.name;

    var name = document.createElement("span");
    name.className = "file-name";
    name.textContent = file.name;
    link.appendChild(name);

    if (file.size_human) {
      var size = document.createElement("span");
      size.className = "muted";
      size.textContent = file.size_human;
      link.appendChild(size);
    }

    if (file.source) {
      var source = document.createElement("span");
      source.className = "tag";
      source.textContent = file.source;
      link.appendChild(source);
    }

    els.fileList.appendChild(link);
  }

  function loadFiles() {
    return api("/api/files").then(function (response) {
      return response.json();
    }).then(function (files) {
      els.fileList.innerHTML = "";
      if (!files.length) {
        var empty = document.createElement("span");
        empty.className = "muted";
        empty.textContent = "No files yet.";
        els.fileList.appendChild(empty);
        return;
      }
      files.forEach(addFileChip);
    }).catch(function () {});
  }

  function uploadFiles(fileList) {
    Array.prototype.forEach.call(fileList, function (file) {
      if (file.size > state.maxUploadMb * 1024 * 1024) {
        showError("File " + file.name + " is larger than " + state.maxUploadMb + " MB.");
        return;
      }
      var data = new FormData();
      data.append("file", file);
      api("/api/upload", { method: "POST", body: data })
        .then(function (response) {
          return response.json().then(function (body) {
            if (!response.ok) {
              throw new Error(body.detail || "Upload failed.");
            }
            return body;
          });
        })
        .then(function (uploaded) {
          state.uploads.push(uploaded);
          renderAttachments();
          loadFiles();
        })
        .catch(function (err) {
          showError(err.message || "Upload failed.");
        });
    });
  }

  // -------------------------------------------------------------- actions
  function sendTask() {
    var text = els.task.value.trim();
    if (!text || state.busy) {
      return;
    }
    addMessage("user", text);
    els.task.value = "";
    els.task.style.height = "";

    api("/api/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task: text,
        files: state.uploads.map(function (file) { return file.id; })
      })
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (!response.ok) {
            throw new Error(body.detail || "Could not start the task.");
          }
          state.uploads = [];
          renderAttachments();
          setBusy(true);
        });
      })
      .catch(function (err) {
        if (err.message !== "redirecting") {
          showError(err.message || "Could not start the task.");
        }
      });
  }

  function stopTask() {
    api("/api/task/stop", { method: "POST" })
      .then(function (response) { return response.json(); })
      .then(function (body) {
        if (!body.ok) {
          showError(body.message || "No task is running.");
        }
      })
      .catch(function () {});
  }

  function reconnectBrowser() {
    els.browserState.textContent = "reconnecting...";
    api("/api/browser/restart", { method: "POST" })
      .then(function (response) { return response.json(); })
      .then(function () {
        refreshStatus();
        state.frameLoaded = false;
        els.frame.src = VNC_URL;
        state.frameLoaded = true;
      })
      .catch(function () {
        refreshStatus();
      });
  }

  // --------------------------------------------------------------- events
  els.send.addEventListener("click", sendTask);
  els.stop.addEventListener("click", stopTask);
  els.reconnect.addEventListener("click", reconnectBrowser);

  els.clear.addEventListener("click", function () {
    els.messages.innerHTML = "";
    addMessage("assistant", "Chat cleared. Describe a new browser task.");
  });

  els.logout.addEventListener("click", function () {
    api("/api/logout", { method: "POST" }).finally(function () {
      window.location.href = "/login";
    });
  });

  els.task.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendTask();
    }
  });

  els.task.addEventListener("input", function () {
    els.task.style.height = "auto";
    els.task.style.height = Math.min(els.task.scrollHeight, 160) + "px";
  });

  els.fileInput.addEventListener("change", function () {
    uploadFiles(els.fileInput.files);
    els.fileInput.value = "";
  });

  // ----------------------------------------------------------------- boot
  refreshStatus();
  loadFiles();
  connectWS();
  window.setInterval(refreshStatus, 10000);
})();
