/**
 * Zustand store for session state management.
 * Server-authoritative: the server sends full ViewState on connect/actions,
 * and deltas during generation. No optimistic updates.
 */

import { create } from "zustand";
import type {
  ChatMessage,
  ClientMessage,
  ViewState,
  ServerMessage,
  PlaybackState,
  ToolCall,
  TargetState,
  ComputedBranchPoint,
} from "@/lib/types";

interface ToolCallRewriteDraft {
  requestId: string;
  status: "rewriting" | "ready" | "error";
  rewrittenArguments?: Record<string, unknown>;
  error?: string;
}

interface SessionState {
  viewState: ViewState | null;
  connectionStatus: "disconnected" | "connecting" | "connected" | "error";
  ws: WebSocket | null;
  sessionId: string | null;
  serverUrl: string;
  version: number;
  reconnectAttempts: number;
  collapsedMessages: Set<string>;
  rewriteDrafts: Record<string, ToolCallRewriteDraft | undefined>;
  lastError: string | null;
  _lastErrorId: number | null;

  // Actions
  connect: (sessionId: string, serverUrl?: string) => void;
  disconnect: () => void;
  reconnect: () => void;
  send: (message: ClientMessage) => void;
  startSession: (initialPrompt: string, auditorModel: string, targetModel: string) => void;
  play: () => void;
  pause: () => void;
  step: () => void;
  sendFeedback: (content: string) => void;
  switchBranch: (branchId: string) => void;
  branchAtMessage: (messageId: string) => void;
  resampleTurn: (turnId: string) => void;
  editToolCall: (toolCallId: string, newArguments: Record<string, unknown>) => void;
  rewriteToolCall: (
    toolCallId: string,
    instruction: string,
    selectedText?: string,
    targetField?: string
  ) => void;
  clearRewriteDraft: (toolCallId: string) => void;
  resampleTargetResponse: (targetMessageId: string, toolCallId?: string) => void;
  toggleMessageCollapsed: (messageId: string) => void;
  clearError: () => void;
  handleServerMessage: (message: ServerMessage) => void;
}

const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAY_MS = 2000;

/**
 * Compute the default WebSocket server URL.
 * - If NEXT_PUBLIC_WS_URL is set, use it.
 * - Otherwise, derive from window.location (same-origin WebSocket via proxy).
 * - Falls back to ws://localhost:8000 for SSR/non-browser contexts.
 */
function getDefaultServerUrl(): string {
  if (typeof window === "undefined") return "ws://localhost:8000";
  const envUrl = process.env.NEXT_PUBLIC_WS_URL;
  if (envUrl) return envUrl;
  // Use same-origin WebSocket (requires proxy rewrite in next.config.js)
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}`;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  // Initial state
  viewState: null,
  connectionStatus: "disconnected",
  ws: null,
  sessionId: null,
  serverUrl: getDefaultServerUrl(),
  version: 0,
  reconnectAttempts: 0,
  collapsedMessages: new Set(),
  rewriteDrafts: {},
  lastError: null,
  _lastErrorId: null,

  // Connect to WebSocket
  connect: (sessionId: string, serverUrl?: string) => {
    if (!serverUrl) serverUrl = getDefaultServerUrl();
    const { ws: existingWs, connectionStatus, sessionId: currentSessionId } = get();

    // Skip if already connecting/connected to the same session
    if (currentSessionId === sessionId && (connectionStatus === "connecting" || connectionStatus === "connected")) {
      console.log(`Already ${connectionStatus} to session ${sessionId}, skipping duplicate connect`);
      return;
    }

    if (existingWs) {
      existingWs.close();
    }

    set({ connectionStatus: "connecting", sessionId, serverUrl });

    const wsUrl = `${serverUrl}/ws/${sessionId}`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      set({ connectionStatus: "connected", ws, reconnectAttempts: 0 });
    };

    ws.onclose = (event) => {
      set({
        connectionStatus: "disconnected",
        ws: null,
      });

      // Auto-reconnect on unexpected close
      if (!event.wasClean) {
        const { sessionId: storedSessionId, reconnectAttempts } = get();
        if (storedSessionId && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
          console.log(`Connection lost. Reconnecting in ${RECONNECT_DELAY_MS}ms... (attempt ${reconnectAttempts + 1}/${MAX_RECONNECT_ATTEMPTS})`);
          set({ reconnectAttempts: reconnectAttempts + 1 });
          setTimeout(() => {
            get().reconnect();
          }, RECONNECT_DELAY_MS);
        }
      }
    };

    ws.onerror = () => {
      set({ connectionStatus: "error" });
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as ServerMessage;
        get().handleServerMessage(message);
      } catch (e) {
        console.error("Failed to parse server message:", e);
        set({ lastError: "Failed to parse server message" });
      }
    };
  },

  // Disconnect WebSocket
  disconnect: () => {
    const { ws } = get();
    if (ws) {
      ws.close();
    }
    set({
      ws: null,
      connectionStatus: "disconnected",
      reconnectAttempts: MAX_RECONNECT_ATTEMPTS, // Prevent auto-reconnect on manual disconnect
    });
  },

  // Reconnect to existing session
  reconnect: () => {
    const { sessionId, serverUrl, connectionStatus } = get();
    if (!sessionId) {
      console.warn("Cannot reconnect: no session ID stored");
      return;
    }
    if (connectionStatus === "connecting" || connectionStatus === "connected") {
      console.warn("Cannot reconnect: already connected or connecting");
      return;
    }
    get().connect(sessionId, serverUrl);
  },

  // Send a message to the server
  send: (message) => {
    const { ws } = get();
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.error(`Cannot send "${message.type}": WebSocket is not connected`);
      set({ lastError: `Cannot send "${message.type}": not connected` });
      return;
    }
    ws.send(JSON.stringify(message));
  },

  // Start a new session
  startSession: (initialPrompt: string, auditorModel: string, targetModel: string) => {
    get().send({
      type: "start_session",
      initial_prompt: initialPrompt,
      auditor_model: auditorModel,
      target_model: targetModel,
    });
  },

  // Playback controls
  play: () => get().send({ type: "play" }),
  pause: () => get().send({ type: "pause" }),
  step: () => get().send({ type: "step" }),

  // Send feedback
  sendFeedback: (content: string) => {
    get().send({ type: "feedback", content });
  },

  // Switch branch
  switchBranch: (branchId: string) => {
    get().send({ type: "switch_branch", branch_id: branchId });
  },

  // Branch at message
  branchAtMessage: (messageId: string) => {
    get().send({ type: "branch", message_id: messageId });
  },

  // Resample turn (regenerate auditor response)
  resampleTurn: (turnId: string) => {
    get().send({ type: "resample_turn", turn_id: turnId });
  },

  // Edit tool call arguments
  editToolCall: (toolCallId: string, newArguments: Record<string, unknown>) => {
    get().send({ type: "edit_tool_call", tool_call_id: toolCallId, new_arguments: newArguments });
  },

  rewriteToolCall: (toolCallId: string, instruction: string, selectedText?: string, targetField?: string) => {
    const { ws } = get();
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      set({
        lastError: 'Cannot send "rewrite_tool_call": not connected',
        rewriteDrafts: {
          ...get().rewriteDrafts,
          [toolCallId]: {
            requestId: "",
            status: "error",
            error: "Not connected",
          },
        },
      });
      return;
    }

    const requestId =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

    set((state) => ({
      rewriteDrafts: {
        ...state.rewriteDrafts,
        [toolCallId]: {
          requestId,
          status: "rewriting",
        },
      },
    }));

    ws.send(JSON.stringify({
      type: "rewrite_tool_call",
      request_id: requestId,
      tool_call_id: toolCallId,
      instruction,
      selected_text: selectedText,
      target_field: targetField,
    }));
  },

  clearRewriteDraft: (toolCallId: string) => {
    set((state) => ({
      rewriteDrafts: {
        ...state.rewriteDrafts,
        [toolCallId]: undefined,
      },
    }));
  },

  // Resample target response
  resampleTargetResponse: (targetMessageId: string, toolCallId?: string) => {
    get().send({
      type: "resample_target_response",
      target_message_id: targetMessageId,
      tool_call_id: toolCallId,
    });
  },

  // Clear error state
  clearError: () => set({ lastError: null, _lastErrorId: null }),

  // Toggle message collapsed state
  toggleMessageCollapsed: (messageId: string) => {
    set((state) => {
      const newCollapsed = new Set(state.collapsedMessages);
      if (newCollapsed.has(messageId)) {
        newCollapsed.delete(messageId);
      } else {
        newCollapsed.add(messageId);
      }
      return { collapsedMessages: newCollapsed };
    });
  },

  // Handle server messages
  handleServerMessage: (message: ServerMessage) => {
    switch (message.type) {
      case "state": {
        set({ viewState: message.state, version: message.state.version, lastError: null });
        break;
      }

      case "delta_turn_start": {
        const { viewState } = get();
        if (!viewState) {
          console.error(`Received ${message.type} before initial state`);
          return;
        }
        if (message.branch_id !== viewState.current_branch.id) return;
        if (message.version <= get().version) return;
        set({
          viewState: {
            ...viewState,
            current_branch: {
              ...viewState.current_branch,
              auditor_messages: [...viewState.current_branch.auditor_messages, message.message],
            },
            is_generating: true,
          },
          version: message.version,
        });
        break;
      }

      case "delta_tool_call": {
        const { viewState } = get();
        if (!viewState) {
          console.error(`Received ${message.type} before initial state`);
          return;
        }
        if (message.branch_id !== viewState.current_branch.id) return;
        if (message.version <= get().version) return;
        const msgs = [...viewState.current_branch.auditor_messages];
        // Find last assistant message
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].role === "assistant") {
            const existingToolCalls = msgs[i].tool_calls;
            if (!existingToolCalls) {
              console.error("tool_calls undefined on assistant message during delta_tool_call — message ordering bug");
              set({ lastError: "Internal error: unexpected message ordering in delta_tool_call" });
            }
            msgs[i] = { ...msgs[i], tool_calls: [...(existingToolCalls ?? []), message.tool_call] };
            break;
          }
        }
        set({
          viewState: {
            ...viewState,
            current_branch: { ...viewState.current_branch, auditor_messages: msgs },
          },
          version: message.version,
        });
        break;
      }

      case "delta_tool_result": {
        const { viewState } = get();
        if (!viewState) {
          console.error(`Received ${message.type} before initial state`);
          return;
        }
        if (message.branch_id !== viewState.current_branch.id) return;
        if (message.version <= get().version) return;
        set({
          viewState: {
            ...viewState,
            current_branch: {
              ...viewState.current_branch,
              auditor_messages: [...viewState.current_branch.auditor_messages, message.tool_result],
              target_state: message.target_state,
            },
          },
          version: message.version,
        });
        break;
      }

      case "rewrite_tool_call_result": {
        set((state) => {
          const current = state.rewriteDrafts[message.tool_call_id];
          if (current && current.requestId !== message.request_id) {
            return state;
          }
          return {
            rewriteDrafts: {
              ...state.rewriteDrafts,
              [message.tool_call_id]: message.error
                ? {
                    requestId: message.request_id,
                    status: "error",
                    error: message.error,
                  }
                : {
                    requestId: message.request_id,
                    status: "ready",
                    rewrittenArguments: message.rewritten_arguments ?? {},
                  },
            },
          };
        });
        break;
      }

      case "error": {
        console.error("Server error:", message.message);
        const errorId = Date.now();
        set({ lastError: message.message, _lastErrorId: errorId });
        setTimeout(() => {
          if (get()._lastErrorId === errorId) {
            set({ lastError: null });
          }
        }, 10000);
        break;
      }

      default: {
        console.error("Unknown server message type:", (message as Record<string, unknown>).type);
        break;
      }
    }
  },
}));

// Stable empty array to avoid creating new references
const EMPTY_BRANCH_POINTS: ComputedBranchPoint[] = [];

// Selector hooks for common state access patterns
export const useViewState = () => useSessionStore((s) => s.viewState);
export const useCurrentBranch = () => useSessionStore((s) => s.viewState?.current_branch ?? null);
export const useIsConnected = () => useSessionStore((s) => s.connectionStatus === "connected");
export const usePlaybackState = () => useSessionStore((s) => s.viewState?.playback_state ?? "idle");
export const useBranchPoints = () => useSessionStore((s) => s.viewState?.branch_points ?? EMPTY_BRANCH_POINTS);
export const useIsGenerating = () =>
  useSessionStore((s) => {
    const vs = s.viewState;
    if (!vs) return false;
    // Derive from playback_state (covers inline generation in resample handlers)
    // AND is_generating (covers background tasks)
    return vs.is_generating || vs.playback_state === "stepping" || vs.playback_state === "playing";
  });

// Fine-grained branch point selectors to avoid re-rendering all messages
export const useTurnBranchPoint = (messageId: string) =>
  useSessionStore((s) => 
    s.viewState?.branch_points?.find(
      (bp) => bp.branch_type === "turn" && bp.message_id === messageId
    ) ?? null
  );

export const useToolCallBranchPoint = (toolCallId: string) =>
  useSessionStore((s) =>
    s.viewState?.branch_points?.find(
      (bp) => bp.branch_type === "tool_call" && bp.tool_call_id === toolCallId
    ) ?? null
  );

export const useTargetBranchPoint = (toolCallId: string) =>
  useSessionStore((s) =>
    s.viewState?.branch_points?.find(
      (bp) => bp.branch_type === "target" && bp.tool_call_id === toolCallId
    ) ?? null
  );

export const useLastError = () => useSessionStore((s) => s.lastError);
export const useToolCallRewriteDraft = (toolCallId: string) =>
  useSessionStore((s) => s.rewriteDrafts[toolCallId] ?? null);
