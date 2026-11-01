import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Send,
  Menu,
  Plus,
  Loader2,
  Check,
  X,
  AlertTriangle,
  Sparkles,
  Film,
  Bot,
  User,
  ChevronDown,
  Trash2,
  Clock,
} from 'lucide-react';
import './index.css';

const HISTORY_KEY = 'manga_drama_history';

const API_BASE = 'http://127.0.0.1:8001';

const STEP_LABELS = {
  clarifying: '需求理解',
  briefed: '需求确认',
  planning: '导演规划',
  writing: '剧本创作',
  reviewing: '内容审核',
  supervising: '导演监督',
  awaiting_human: '人工审核',
  anchors_pending: '锚定图生成',
  confirming_anchors: '锚定图确认',
  producing: '视频制作',
  done: '制作完成',
  failed: '流程终止',
};

const EXAMPLES = [
  '做一部黛玉葬花的古风漫剧，30秒，哀伤基调',
  '三国演义草船借箭片段，紧张悬疑风格',
  '赛博朋克都市的短篇科幻故事，未来感十足',
  '小猫咪的日常治愈系漫剧，温馨可爱',
];

// ── 流式文本 Hook ──
function useStreamingText(fullText, speed = 15) {
  const [displayed, setDisplayed] = useState('');
  const [done, setDone] = useState(false);
  const timerRef = useRef(null);

  useEffect(() => {
    if (!fullText) {
      setDisplayed('');
      setDone(false);
      return;
    }
    let idx = 0;
    setDisplayed('');
    setDone(false);

    timerRef.current = setInterval(() => {
      idx += 3; // 每次 3 字符，平衡流畅度与速度
      if (idx >= fullText.length) {
        setDisplayed(fullText);
        setDone(true);
        clearInterval(timerRef.current);
      } else {
        setDisplayed(fullText.slice(0, idx));
      }
    }, speed);

    return () => clearInterval(timerRef.current);
  }, [fullText, speed]);

  return { displayed, done };
}

// ── 侧边栏组件 ──
function Sidebar({ collapsed, onToggle, onNewChat, history, activeJobId, onSelect, onDelete }) {
  const formatTime = (ts) => {
    const d = new Date(ts);
    const now = new Date();
    const isToday = d.toDateString() === now.toDateString();
    if (isToday) return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
  };

  return (
    <div className={`sidebar ${collapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-header">
        <div className="sidebar-logo">
          <div className="sidebar-logo-icon">🎬</div>
          <span className="sidebar-logo-text">AI 漫剧工坊</span>
        </div>
        <button className="btn-new-chat" onClick={onNewChat}>
          <Plus size={16} />
          新对话
        </button>
      </div>
      <div className="sidebar-history">
        {history.length === 0 && (
          <div className="history-empty">暂无历史对话</div>
        )}
        {history.map((h) => (
          <div
            key={h.id}
            className={`history-item ${h.jobId === activeJobId ? 'active' : ''}`}
            onClick={() => onSelect(h)}
          >
            <Film size={14} style={{ marginRight: 8, flexShrink: 0 }} />
            <div className="history-item-content">
              <span className="history-item-title">{h.title}</span>
              <span className="history-item-time">
                <Clock size={10} style={{ marginRight: 3 }} />
                {formatTime(h.timestamp)}
              </span>
            </div>
            <button
              className="history-delete"
              onClick={(e) => { e.stopPropagation(); onDelete(h.id); }}
              title="删除"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── 进度指示器组件 ──
function ProgressIndicator({ statusText, percentage, currentStatus }) {
  const steps = Object.entries(STEP_LABELS);
  const statusOrder = [
    'clarifying', 'briefed', 'planning', 'writing', 'reviewing',
    'supervising', 'awaiting_human', 'anchors_pending', 'confirming_anchors',
    'producing', 'done', 'failed',
  ];
  const currentIdx = statusOrder.indexOf(currentStatus);

  return (
    <div className="progress-indicator">
      <div className="progress-header">
        <div className="progress-icon">
          <Loader2 size={18} className="spinner" style={{ color: 'var(--accent)' }} />
        </div>
        <span className="progress-status">{statusText || 'AI 正在工作中...'}</span>
        {percentage > 0 && (
          <span className="progress-percent">{percentage}%</span>
        )}
      </div>
      {percentage > 0 && (
        <div className="progress-bar">
          <div className="progress-bar-fill" style={{ width: `${percentage}%` }} />
        </div>
      )}
      <div className="progress-steps">
        {statusOrder.slice(0, -2).map((key, idx) => {
          let cls = 'progress-step';
          if (idx < currentIdx) cls += ' completed';
          else if (idx === currentIdx) cls += ' active';
          return (
            <span key={key} className={cls}>
              {STEP_LABELS[key]}
            </span>
          );
        })}
      </div>
    </div>
  );
}

// ── 剧本消息组件（带流式显示 + 审核按钮）──
function DraftMessage({ draft, outline, machineVerdict, directorFeedback, onApprove, onReject, loading }) {
  // draft 可能是对象，安全转为字符串
  const draftStr = typeof draft === 'object'
    ? JSON.stringify(draft, null, 2)
    : String(draft || '');
  const { displayed, done } = useStreamingText(draftStr, 12);
  const [showRejectInput, setShowRejectInput] = useState(false);
  const [rejectFeedback, setRejectFeedback] = useState('');

  return (
    <div className="message-content draft-message">
      <div style={{ fontWeight: 600, marginBottom: 8, color: 'var(--accent)' }}>
        📝 剧本创作完成
      </div>

      {/* 审核信息 */}
      {machineVerdict && (
        <div className="review-info">
          <div className="review-info-label">机器审核</div>
          <div className="review-info-value">
            {machineVerdict.passed ? '✅ 通过' : '❌ 未通过'}
            {machineVerdict.issues?.length > 0 && ` — ${machineVerdict.issues.join('、')}`}
          </div>
        </div>
      )}
      {directorFeedback && (
        <div className="review-info">
          <div className="review-info-label">导演意见</div>
          <div className="review-info-value">
            {typeof directorFeedback === 'object'
              ? JSON.stringify(directorFeedback, null, 2)
              : directorFeedback}
          </div>
        </div>
      )}

      {/* 剧本内容（流式显示） */}
      <div className={`draft-text ${done ? '' : 'streaming'}`}>
        {displayed}
      </div>

      {done && (
        <div className="draft-meta">
          <span>·</span>
          <span>{draftStr?.length || 0} 字</span>
        </div>
      )}

      {/* 操作按钮 */}
      {done && (
        <div className="draft-actions">
          {!showRejectInput ? (
            <>
              <button
                className="btn btn-success"
                onClick={onApprove}
                disabled={loading}
              >
                <Check size={15} /> 批准剧本
              </button>
              <button
                className="btn btn-error"
                onClick={() => setShowRejectInput(true)}
                disabled={loading}
              >
                <X size={15} /> 驳回修订
              </button>
            </>
          ) : (
            <div style={{ width: '100%' }}>
              <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
                <textarea
                  className="input-box"
                  style={{
                    flex: 1, padding: '8px 12px', borderRadius: 'var(--radius-sm)',
                    background: 'var(--bg-input)', border: '1px solid var(--border)',
                    color: 'var(--text-primary)', fontFamily: 'inherit', fontSize: 13,
                    resize: 'none', minHeight: 38,
                  }}
                  rows={2}
                  placeholder="请描述需要修改的内容（如：把第二场台词改得更哀伤）..."
                  value={rejectFeedback}
                  onChange={(e) => setRejectFeedback(e.target.value)}
                />
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  className="btn btn-error"
                  onClick={() => onReject(rejectFeedback || '请按意见修订')}
                  disabled={loading}
                >
                  <X size={15} /> 提交驳回
                </button>
                <button
                  className="btn btn-ghost"
                  onClick={() => { setShowRejectInput(false); setRejectFeedback(''); }}
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── 锚定图确认组件 ──
function getAssetUrl(fullPath) {
  // 从完整路径中提取 job_id 和文件名，构造 /assets/<job_id>/<filename> URL
  // 路径格式：data/drama_assets/<job_id>/<filename> 或 Windows 反斜杠格式
  if (!fullPath) return '';
  const parts = fullPath.replace(/\\/g, '/').split('/');
  const assetsIdx = parts.lastIndexOf('drama_assets');
  if (assetsIdx >= 0 && assetsIdx + 2 < parts.length) {
    const jobId = parts[assetsIdx + 1];
    const filename = parts[parts.length - 1];
    return `${API_BASE}/assets/${jobId}/${filename}`;
  }
  // fallback: 取最后两个路径段
  const filename = parts[parts.length - 1];
  return `${API_BASE}/output/${filename}`;
}

function AnchorConfirmMessage({ anchors, onApprove, onReject, loading }) {
  return (
    <div className="message-content draft-message">
      <div style={{ fontWeight: 600, marginBottom: 8, color: 'var(--accent)' }}>
        🖼️ 角色外观确认
      </div>
      <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
        系统已为角色生成锚定图，请确认外观（确认后将全片锁定）。
      </p>
      <div className="anchor-grid">
        {anchors?.map((a, i) => (
          <div key={i} className="anchor-card">
            <img
              src={getAssetUrl(a.anchor_image_path)}
              alt={a.name}
              onError={(e) => { e.target.style.display = 'none'; }}
            />
            <div className="anchor-card-name">{a.name}</div>
          </div>
        ))}
      </div>
      <div className="draft-actions">
        <button className="btn btn-success" onClick={onApprove} disabled={loading}>
          <Check size={15} /> 批准外观
        </button>
        <button className="btn btn-error" onClick={onReject} disabled={loading}>
          <X size={15} /> 要求重画
        </button>
      </div>
    </div>
  );
}

// ── 消息组件 ──
function Message({ msg }) {
  const isUser = msg.role === 'user';
  const isSystem = msg.role === 'system';

  return (
    <div className={`message ${msg.role}`}>
      <div className="message-avatar">
        {isUser ? <User size={18} /> : isSystem ? <Sparkles size={16} /> : <Bot size={18} />}
      </div>
      <div className="message-body">
        {msg.type === 'draft' ? (
          <DraftMessage
            draft={msg.draft}
            outline={msg.outline}
            machineVerdict={msg.machineVerdict}
            directorFeedback={msg.directorFeedback}
            onApprove={msg.onApprove}
            onReject={msg.onReject}
            loading={msg.loading}
          />
        ) : msg.type === 'anchor_confirm' ? (
          <AnchorConfirmMessage
            anchors={msg.anchors}
            onApprove={msg.onApprove}
            onReject={msg.onReject}
            loading={msg.loading}
          />
        ) : msg.type === 'video' ? (
          <div className="message-content">
            <div className="completion-panel">
              <h3>🎉 视频生成成功！</h3>
              <video
                controls
                src={`${API_BASE}/output/${msg.videoPath?.split('/').pop()}`}
              />
            </div>
          </div>
        ) : msg.type === 'progress' ? (
          <ProgressIndicator
            statusText={msg.statusText}
            percentage={msg.percentage}
            currentStatus={msg.currentStatus}
          />
        ) : msg.type === 'error' ? (
          <div className="error-panel">
            <AlertTriangle size={18} className="error-panel-icon" />
            <div className="error-panel-text">{msg.content}</div>
          </div>
        ) : (
          <div className="message-content">
            {msg.content}
          </div>
        )}
      </div>
    </div>
  );
}

// ── 欢迎界面 ──
function Welcome({ onExampleClick }) {
  return (
    <div className="welcome">
      <div className="welcome-icon">
        <Film size={36} color="white" />
      </div>
      <h1>AI 漫剧工坊</h1>
      <p>多 Agent 协作的一站式漫剧制作平台</p>
      <div className="welcome-examples">
        {EXAMPLES.map((ex, i) => (
          <div key={i} className="example-chip" onClick={() => onExampleClick(ex)}>
            {ex}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── 主应用 ──
export default function App() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [idea, setIdea] = useState('');
  const [jobId, setJobId] = useState(null);
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState([]);
  const [history, setHistory] = useState([]);
  const messageListRef = useRef(null);
  const eventSourceRef = useRef(null);
  const textareaRef = useRef(null);
  // 用 ref 保存最新的操作函数，避免 handleInterrupt 闭包过时
  const handleApproveDraftRef = useRef(null);
  const handleRejectDraftRef = useRef(null);
  const handleApproveAnchorsRef = useRef(null);
  const handleRejectAnchorsRef = useRef(null);

  // ── 对话历史管理 ──
  // 从 localStorage 加载历史
  useEffect(() => {
    try {
      const saved = localStorage.getItem(HISTORY_KEY);
      if (saved) setHistory(JSON.parse(saved));
    } catch {}
  }, []);

  // 保存历史到 localStorage
  const saveHistory = useCallback((newHistory) => {
    setHistory(newHistory);
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(newHistory));
    } catch {}
  }, []);

  // 自动保存当前会话（每次 messages 变化时）
  useEffect(() => {
    if (!jobId || messages.length === 0) return;
    // 只保存有实际内容的会话
    const hasUserMsg = messages.some((m) => m.role === 'user');
    if (!hasUserMsg) return;
    const title = messages.find((m) => m.role === 'user')?.content?.slice(0, 30) || '未命名创作';
    saveHistory((prev) => {
      const exists = prev.findIndex((h) => h.jobId === jobId);
      const entry = { id: jobId, title, jobId, timestamp: Date.now(), messages };
      if (exists >= 0) {
        const updated = [...prev];
        updated[exists] = entry;
        return updated;
      }
      return [entry, ...prev].slice(0, 20); // 最多保留 20 条
    });
  }, [messages, jobId, saveHistory]);

  // 选择历史对话
  const handleSelectHistory = useCallback((item) => {
    setJobId(item.jobId);
    setMessages(item.messages || []);
    setIdea('');
    setLoading(false);
  }, []);

  // 删除历史对话
  const handleDeleteHistory = useCallback((id) => {
    saveHistory((prev) => prev.filter((h) => h.id !== id));
  }, [saveHistory]);

  // 自动滚动到底部
  const scrollToBottom = useCallback(() => {
    if (messageListRef.current) {
      messageListRef.current.scrollTop = messageListRef.current.scrollHeight;
    }
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  // SSE 连接管理
  useEffect(() => {
    if (!jobId) return;

    const es = new EventSource(`${API_BASE}/api/jobs/${jobId}/stream`);
    eventSourceRef.current = es;

    es.addEventListener('progress', (e) => {
      try {
        const data = JSON.parse(e.data);
        setSnapshot((prev) => {
          if (!prev) return prev;
          return {
            ...prev,
            progress: data,
            values: { ...prev.values, status: data.currentStatus || prev.values?.status },
          };
        });
        // 更新或添加进度消息（如果有 draft 消息则不添加新的进度消息）
        setMessages((prev) => {
          const hasDraft = prev.some((m) => m.type === 'draft');
          const lastMsg = prev[prev.length - 1];
          if (lastMsg?.type === 'progress') {
            return [
              ...prev.slice(0, -1),
              { ...lastMsg, statusText: data.status_text, percentage: data.percentage },
            ];
          }
          if (hasDraft) return prev; // 已有剧本，不添加进度消息
          return [
            ...prev,
            {
              id: `progress-${Date.now()}`,
              role: 'system',
              type: 'progress',
              statusText: data.status_text,
              percentage: data.percentage,
            },
          ];
        });
      } catch {}
    });

    es.addEventListener('draft_ready', (e) => {
      try {
        const data = JSON.parse(e.data);
        // outline 可能是对象，确保转为字符串
        const outlineStr = typeof data.outline === 'object'
          ? JSON.stringify(data.outline, null, 2)
          : data.outline || '';
        // 移除进度消息，添加剧本消息（如果还没有的话）
        setMessages((prev) => {
          const hasDraft = prev.some((m) => m.type === 'draft');
          if (hasDraft) return prev; // 已有剧本消息，不重复添加
          const withoutProgress = prev.filter((m) => m.type !== 'progress');
          return [
            ...withoutProgress,
            {
              id: `draft-${Date.now()}`,
              role: 'assistant',
              type: 'draft',
              draft: data.draft,
              outline: outlineStr,
              machineVerdict: null,
              directorFeedback: null,
              onApprove: () => handleApproveDraftRef.current?.(),
              onReject: (fb) => handleRejectDraftRef.current?.(fb),
              loading: false,
            },
          ];
        });
      } catch {}
    });

    es.addEventListener('review_done', (e) => {
      try {
        const data = JSON.parse(e.data);
        // 更新最近的 draft 消息的审核信息
        setMessages((prev) => {
          const updated = [...prev];
          for (let i = updated.length - 1; i >= 0; i--) {
            if (updated[i].type === 'draft') {
              updated[i] = {
                ...updated[i],
                machineVerdict: { passed: data.passed, issues: data.issues },
              };
              break;
            }
          }
          return updated;
        });
      } catch {}
    });

    es.addEventListener('supervise_done', (e) => {
      try {
        const data = JSON.parse(e.data);
        setMessages((prev) => {
          const updated = [...prev];
          for (let i = updated.length - 1; i >= 0; i--) {
            if (updated[i].type === 'draft') {
              updated[i] = {
                ...updated[i],
                directorFeedback: data.feedback,
              };
              break;
            }
          }
          return updated;
        });
      } catch {}
    });

    es.addEventListener('interrupt', (e) => {
      try {
        const data = JSON.parse(e.data);
        setSnapshot((prev) => ({ ...prev, interrupt: data }));
        handleInterrupt(data);
      } catch {}
    });

    es.addEventListener('done', (e) => {
      try {
        const data = JSON.parse(e.data);
        setMessages((prev) => {
          const withoutProgress = prev.filter((m) => m.type !== 'progress');
          return [
            ...withoutProgress,
            {
              id: `video-${Date.now()}`,
              role: 'assistant',
              type: 'video',
              videoPath: data.video_path,
            },
          ];
        });
        setSnapshot((prev) => ({ ...prev, done: true }));
        setLoading(false);
      } catch {}
    });

    es.addEventListener('error', (e) => {
      try {
        const data = JSON.parse(e.data);
        setMessages((prev) => {
          const withoutProgress = prev.filter((m) => m.type !== 'progress');
          return [
            ...withoutProgress,
            {
              id: `error-${Date.now()}`,
              role: 'system',
              type: 'error',
              content: data.error || '未知错误',
            },
          ];
        });
        setLoading(false);
      } catch {}
    });

    es.onerror = () => {
      // SSE 连接错误时回退到轮询
      console.warn('SSE connection error, will retry...');
    };

    return () => {
      es.close();
      eventSourceRef.current = null;
    };
  }, [jobId]);

  // 处理中断事件（使用 ref 调用，避免闭包过时）
  const handleInterrupt = useCallback((interruptData) => {
    if (!interruptData) return;
    const { type, question, missing, draft, machine_verdict, director_feedback, anchors } = interruptData;

    if (type === 'clarify') {
      // 澄清问题 — 显示问题 + 引导提示
      const missingText = missing?.length > 0 ? `\n\n待确认维度：${missing.join('、')}` : '';
      setMessages((prev) => [
        ...prev.filter((m) => m.type !== 'progress'),
        {
          id: `clarify-${Date.now()}`,
          role: 'assistant',
          type: 'text',
          content: `💬 ${question}${missingText}`,
        },
        {
          id: `clarify-hint-${Date.now()}`,
          role: 'system',
          type: 'text',
          content: '💡 请在下方输入框直接回答上述问题。',
        },
      ]);
      setLoading(false); // 允许输入回答
    } else if (type === 'human_review') {
      // 人工审核 — 更新已有剧本消息的审核信息，或创建新消息
      setMessages((prev) => {
        // 查找已有的 draft 消息并更新
        const updated = prev.map((m) => {
          if (m.type === 'draft' && !m.machineVerdict) {
            return {
              ...m,
              draft: draft || m.draft,
              machineVerdict: machine_verdict,
              directorFeedback: director_feedback,
              onApprove: () => handleApproveDraftRef.current?.(),
              onReject: (fb) => handleRejectDraftRef.current?.(fb),
              loading: false,
            };
          }
          return m;
        });
        const hasDraft = updated.some((m) => m.type === 'draft');
        if (hasDraft) {
          return updated.filter((m) => m.type !== 'progress');
        }
        // 没有已有 draft 消息，创建新的
        return [
          ...updated.filter((m) => m.type !== 'progress'),
          {
            id: `review-${Date.now()}`,
            role: 'assistant',
            type: 'draft',
            draft: draft,
            machineVerdict: machine_verdict,
            directorFeedback: director_feedback,
            onApprove: () => handleApproveDraftRef.current?.(),
            onReject: (fb) => handleRejectDraftRef.current?.(fb),
            loading: false,
          },
        ];
      });
      setLoading(false); // 允许输入修改意见
    } else if (type === 'confirm_anchors') {
      // 锚定图确认
      setMessages((prev) => {
        const withoutProgress = prev.filter((m) => m.type !== 'progress');
        return [
          ...withoutProgress,
          {
            id: `anchor-${Date.now()}`,
            role: 'assistant',
            type: 'anchor_confirm',
            anchors: anchors,
            onApprove: () => handleApproveAnchorsRef.current?.(),
            onReject: () => handleRejectAnchorsRef.current?.(),
            loading: false,
          },
        ];
      });
      setLoading(false);
    }
  }, []);

  // ── 操作处理 ──

  const startJob = async () => {
    if (!idea.trim()) return;
    const userIdea = idea.trim();
    setIdea('');
    setLoading(true);

    // 添加用户消息
    setMessages((prev) => [
      ...prev,
      { id: `user-${Date.now()}`, role: 'user', type: 'text', content: userIdea },
    ]);

    try {
      const res = await fetch(`${API_BASE}/api/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ idea: userIdea }),
      });
      const data = await res.json();
      setJobId(data.job_id);
      setSnapshot(data.snapshot);
    } catch (e) {
      console.error(e);
      setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: 'system',
          type: 'error',
          content: '无法连接到后端服务器，请确认服务已启动。',
        },
      ]);
      setLoading(false);
    }
  };

  const handleApproveDraft = async () => {
    if (!jobId) return;
    setLoading(true);
    setMessages((prev) => [
      ...prev,
      { id: `user-approve-${Date.now()}`, role: 'user', type: 'text', content: '✅ 批准剧本' },
    ]);
    try {
      // 先获取最新 snapshot 确保 interrupt 状态同步
      const snapRes = await fetch(`${API_BASE}/api/jobs/${jobId}`);
      if (snapRes.ok) {
        const snapData = await snapRes.json();
        setSnapshot(snapData.snapshot);
      }
      const res = await fetch(`${API_BASE}/api/jobs/${jobId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: { approved: true, feedback: '' } }),
      });
      const data = await res.json();
      setSnapshot(data.snapshot);
      // 清除旧的 draft 消息的按钮 + 添加实时动态提示
      setMessages((prev) => [
        ...prev.map((m) =>
          m.type === 'draft' ? { ...m, onApprove: undefined, onReject: undefined, loading: true } : m
        ),
        { id: `working-${Date.now()}`, role: 'system', type: 'text', content: '⏳ AI 正在生成角色锚定图，请稍候...' },
      ]);
    } catch (e) {
      console.error(e);
      setLoading(false);
    }
  };

  const handleRejectDraft = async (feedback) => {
    if (!jobId) return;
    setLoading(true);
    setMessages((prev) => [
      ...prev,
      {
        id: `user-reject-${Date.now()}`,
        role: 'user',
        type: 'text',
        content: `❌ 驳回：${feedback}`,
      },
    ]);
    try {
      // 先获取最新 snapshot 确保 interrupt 状态同步
      const snapRes = await fetch(`${API_BASE}/api/jobs/${jobId}`);
      if (snapRes.ok) {
        const snapData = await snapRes.json();
        setSnapshot(snapData.snapshot);
      }
      const res = await fetch(`${API_BASE}/api/jobs/${jobId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: { approved: false, feedback } }),
      });
      const data = await res.json();
      setSnapshot(data.snapshot);
      // 清除旧的 draft 消息的按钮 + 添加实时动态提示
      setMessages((prev) => [
        ...prev.map((m) =>
          m.type === 'draft' ? { ...m, onApprove: undefined, onReject: undefined, loading: true } : m
        ),
        { id: `working-${Date.now()}`, role: 'system', type: 'text', content: '⏳ AI 正在根据您的意见修改剧本，请稍候...' },
      ]);
    } catch (e) {
      console.error(e);
      setLoading(false);
    }
  };

  const handleApproveAnchors = async () => {
    if (!jobId) return;
    setLoading(true);
    setMessages((prev) => [
      ...prev,
      { id: `user-approve-a-${Date.now()}`, role: 'user', type: 'text', content: '✅ 批准角色外观' },
      { id: `working-v-${Date.now()}`, role: 'system', type: 'text', content: '⏳ AI 正在生成分镜画面与配音，预计需要几分钟...' },
    ]);
    try {
      // 先获取最新 snapshot 确保 interrupt 状态同步
      const snapRes = await fetch(`${API_BASE}/api/jobs/${jobId}`);
      if (snapRes.ok) {
        const snapData = await snapRes.json();
        setSnapshot(snapData.snapshot);
      }
      const res = await fetch(`${API_BASE}/api/jobs/${jobId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: { approved: true } }),
      });
      const data = await res.json();
      setSnapshot(data.snapshot);
    } catch (e) {
      console.error(e);
      setLoading(false);
    }
  };

  const handleRejectAnchors = async () => {
    if (!jobId) return;
    setLoading(true);
    setMessages((prev) => [
      ...prev,
      { id: `user-reject-a-${Date.now()}`, role: 'user', type: 'text', content: '❌ 要求重画' },
      { id: `working-r-${Date.now()}`, role: 'system', type: 'text', content: '⏳ AI 正在重新生成角色锚定图，请稍候...' },
    ]);
    try {
      // 先获取最新 snapshot 确保 interrupt 状态同步
      const snapRes = await fetch(`${API_BASE}/api/jobs/${jobId}`);
      if (snapRes.ok) {
        const snapData = await snapRes.json();
        setSnapshot(snapData.snapshot);
      }
      const res = await fetch(`${API_BASE}/api/jobs/${jobId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: { approved: false } }),
      });
      const data = await res.json();
      setSnapshot(data.snapshot);
    } catch (e) {
      console.error(e);
      setLoading(false);
    }
  };

  // 更新 ref，确保 handleInterrupt 闭包中引用最新的函数
  handleApproveDraftRef.current = handleApproveDraft;
  handleRejectDraftRef.current = handleRejectDraft;
  handleApproveAnchorsRef.current = handleApproveAnchors;
  handleRejectAnchorsRef.current = handleRejectAnchors;

  const handleSendMessage = async () => {
    if (!idea.trim()) return;
    const msg = idea.trim();
    setIdea('');

    // 添加用户消息
    setMessages((prev) => [
      ...prev,
      { id: `user-${Date.now()}`, role: 'user', type: 'text', content: msg },
    ]);

    if (!jobId) {
      // 无任务 — 启动新任务
      setLoading(true);
      try {
        const res = await fetch(`${API_BASE}/api/jobs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ idea: msg }),
        });
        const data = await res.json();
        setJobId(data.job_id);
        setSnapshot(data.snapshot);
      } catch (e) {
        console.error(e);
        setMessages((prev) => [
          ...prev,
          {
            id: `error-${Date.now()}`,
            role: 'system',
            type: 'error',
            content: '无法连接到后端服务器',
          },
        ]);
        setLoading(false);
      }
      return;
    }

    // 有任务 — 根据中断类型处理
    const interrupt = snapshot?.interrupt;
    if (interrupt?.type === 'clarify') {
      // 回答澄清问题
      setLoading(true);
      try {
        const res = await fetch(`${API_BASE}/api/jobs/${jobId}/resume`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: msg }),
        });
        const data = await res.json();
        setSnapshot(data.snapshot);
      } catch (e) {
        console.error(e);
        setLoading(false);
      }
    } else if (interrupt?.type === 'human_review') {
      // 在审核中断中输入文字 — 视为驳回 + 修改意见
      handleRejectDraft(msg);
    } else if (interrupt?.type === 'confirm_anchors') {
      // 锚定图确认中输入文字 — 视为要求重画
      handleRejectAnchors();
    } else {
      // 流程进行中（无中断）— 暂存修改建议
      setMessages((prev) => [
        ...prev,
        {
          id: `system-${Date.now()}`,
          role: 'system',
          type: 'text',
          content: '⏳ 当前流程正在进行中，您的建议已记录。待流程暂停时请再次输入。',
        },
      ]);
    }
  };

  const handleNewChat = () => {
    setJobId(null);
    setSnapshot(null);
    setMessages([]);
    setIdea('');
    setLoading(false);
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
  };

  const handleExampleClick = (example) => {
    setIdea(example);
    if (textareaRef.current) {
      textareaRef.current.focus();
    }
  };

  // 计算当前状态
  const currentStatus = snapshot?.values?.status;
  const isDone = snapshot?.done;
  const isFailed = currentStatus === 'failed';
  const hasInterrupt = !!snapshot?.interrupt;
  const cost = snapshot?.values?.budget_events
    ? snapshot.values.budget_events.reduce((acc, e) => acc + (parseFloat(e.cost_cny) || 0), 0)
    : 0;

  // 输入框是否禁用：
  // - 无任务时：不禁用（可以输入创意启动新任务）
  // - 有中断时：不禁用（用户需要回答问题/输入修改意见）
  // - 有任务无中断 + loading=true：禁用（agent 正在工作）
  // - 有任务无中断 + loading=false：禁用（流程自动进行中，等待下次中断）
  const isInputDisabled = !!jobId && !hasInterrupt;

  // 输入框 placeholder
  const getPlaceholder = () => {
    if (!jobId) return '描述你的漫剧构思...';
    if (hasInterrupt) {
      if (snapshot.interrupt.type === 'clarify') return '请回答上述问题...';
      if (snapshot.interrupt.type === 'human_review') return '输入修改意见，或点击上方按钮批准/驳回...';
      if (snapshot.interrupt.type === 'confirm_anchors') return '点击上方按钮确认角色外观...';
    }
    if (loading) return 'AI 正在创作中，请稍候...';
    return '输入修改建议或新想法...';
  };

  return (
    <>
      <Sidebar
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
        onNewChat={handleNewChat}
        history={history}
        activeJobId={jobId}
        onSelect={handleSelectHistory}
        onDelete={handleDeleteHistory}
      />

      <div className="main-content">
        {/* 顶栏 */}
        <div className="chat-header">
          <div className="chat-header-left">
            <button
              className="btn-toggle-sidebar"
              onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
            >
              <Menu size={18} />
            </button>
            <span className="header-title">
              {jobId ? (STEP_LABELS[currentStatus] || '创作中') : 'AI 漫剧工坊'}
            </span>
          </div>
          {cost > 0 && (
            <span className="header-cost">¥{cost.toFixed(4)}</span>
          )}
        </div>

        {/* 消息区 */}
        {messages.length === 0 ? (
          <Welcome onExampleClick={handleExampleClick} />
        ) : (
          <div className="message-list" ref={messageListRef}>
            <div className="message-container">
              {messages.map((msg) => (
                <Message key={msg.id} msg={msg} />
              ))}

              {/* 完成/失败面板已在消息中内联 */}

              {/* 底部间距 */}
              <div style={{ height: 20 }} />
            </div>
          </div>
        )}

        {/* 输入区 */}
        <div className="input-area">
          <div className="input-wrapper">
            <div className="input-box">
              <textarea
                ref={textareaRef}
                rows={1}
                placeholder={getPlaceholder()}
                value={idea}
                onChange={(e) => {
                  setIdea(e.target.value);
                  // 自动调整高度
                  e.target.style.height = 'auto';
                  e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey && !isInputDisabled) {
                    e.preventDefault();
                    handleSendMessage();
                  }
                }}
                disabled={isInputDisabled}
              />
              <button
                className="btn-send"
                onClick={handleSendMessage}
                disabled={!idea.trim() || isInputDisabled}
              >
                {loading ? <Loader2 size={18} className="spinner" /> : <Send size={18} />}
              </button>
            </div>
            <div className="input-hint">
              Shift + Enter 换行 · Enter 发送
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
