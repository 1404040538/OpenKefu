import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { BookOpen, Edit3, FileText, Image as ImageIcon, Loader2, Plus, Save, Trash2, UploadCloud, X } from "lucide-react";
import type { KnowledgeBase, KnowledgeQaItem, NoteSet, NoteSetItem, Shop, User } from "../types/types";
import { StatusBadge } from "./StatusBadge";
import { formatBytes } from "../utils/helpers";
import { usePendingActions } from "../utils/usePendingActions";

type ContentType = "kb" | "ns";
type MixedItem = { type: "kb"; data: KnowledgeBase } | { type: "ns"; data: NoteSet };
type EditableKnowledgeQaItem = KnowledgeQaItem & { _localId: string };
const MAX_SERVICE_KNOWLEDGE_FILES = 3;
const MAX_SERVICE_FILE_BYTES = 20 * 1024 * 1024;

function itemKey(item: MixedItem) {
  return `${item.type}-${item.data.id}`;
}

export function KnowledgePage({
  user,
  shops,
  knowledgeBases,
  noteSets,
  onRefresh,
  onLoad,
  onLoadNoteSet,
  onCreate,
  onUpdate,
  onDelete,
  onUpload,
  onSaveQaItems,
  onCreateNoteSet,
  onUpdateNoteSet,
  onDeleteNoteSet,
  onAddNoteItem,
  onUpdateNoteItem,
  onDeleteNoteItem,
}: {
  user: User;
  shops: Shop[];
  knowledgeBases: KnowledgeBase[];
  noteSets: NoteSet[];
  onRefresh: () => Promise<void>;
  onLoad: (id: number) => Promise<KnowledgeBase>;
  onLoadNoteSet: (id: number) => Promise<NoteSet>;
  onCreate: (body: any) => Promise<void>;
  onUpdate: (id: number, body: any) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
  onUpload: (id: number, file: File) => Promise<void>;
  onSaveQaItems: (id: number, items: KnowledgeQaItem[]) => Promise<void>;
  onCreateNoteSet: (body: any) => Promise<void>;
  onUpdateNoteSet: (id: number, body: any) => Promise<void>;
  onDeleteNoteSet: (id: number) => Promise<void>;
  onAddNoteItem: (nsId: number, content: string) => Promise<void>;
  onUpdateNoteItem: (nsId: number, itemId: number, content: string) => Promise<void>;
  onDeleteNoteItem: (nsId: number, itemId: number) => Promise<void>;
}) {
  const [selectedKey, setSelectedKey] = useState("");
  const [detail, setDetail] = useState<KnowledgeBase | NoteSet | null>(null);
  const [shopIds, setShopIds] = useState<number[]>([]);
  const [createType, setCreateType] = useState<ContentType>("kb");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [createShopIds, setCreateShopIds] = useState<number[]>([]);
  const [qaRows, setQaRows] = useState<EditableKnowledgeQaItem[]>([]);
  const [newNoteContent, setNewNoteContent] = useState("");
  const [editingNoteId, setEditingNoteId] = useState<number | null>(null);
  const [editingNoteContent, setEditingNoteContent] = useState("");
  const [busy, setBusy] = useState(false);
  const { runAction } = usePendingActions();

  const canManage = user.role === "admin" || user.role === "service";
  const mixedList = useMemo<MixedItem[]>(() => {
    const rows: MixedItem[] = [
      ...knowledgeBases.map((data) => ({ type: "kb" as const, data })),
      ...noteSets.map((data) => ({ type: "ns" as const, data })),
    ];
    return rows.sort((a, b) => b.data.id - a.data.id);
  }, [knowledgeBases, noteSets]);
  const selected = mixedList.find((item) => itemKey(item) === selectedKey) || mixedList[0] || null;
  const isKnowledgeBase = selected?.type === "kb";
  const status = detail?.status || selected?.data.status || "active";
  const maxKnowledgeBases = user.role === "admin" ? null : user.max_knowledge_bases ?? 5;
  const canCreateKnowledgeBase = maxKnowledgeBases == null || knowledgeBases.length < maxKnowledgeBases;
  const selectedFileCount = isKnowledgeBase ? ((detail as KnowledgeBase | null)?.files || []).length : 0;
  const canUploadKnowledgeFile = user.role === "admin" || selectedFileCount < MAX_SERVICE_KNOWLEDGE_FILES;

  useEffect(() => {
    if (selected && selectedKey !== itemKey(selected)) setSelectedKey(itemKey(selected));
  }, [selected, selectedKey]);

  useEffect(() => {
    let cancelled = false;
    if (!selected) {
      setDetail(null);
      return;
    }
    const loader = selected.type === "kb" ? onLoad(selected.data.id) : onLoadNoteSet(selected.data.id);
    loader.then((row) => { if (!cancelled) setDetail(row); }).catch(() => { if (!cancelled) setDetail(selected.data); });
    return () => { cancelled = true; };
  }, [selected?.type, selected?.data.id]);

  useEffect(() => {
    setShopIds(detail?.shop_ids || []);
  }, [detail?.id, selected?.type]);

  useEffect(() => {
    if (!isKnowledgeBase || !detail) {
      setQaRows([]);
      return;
    }
    const kbDetail = detail as KnowledgeBase;
    setQaRows((kbDetail.qa_items || []).map((item, index) => ({
      ...item,
      _localId: `qa-${item.id || "new"}-${index}`,
    })));
  }, [detail, isKnowledgeBase]);

  async function reloadSelected() {
    await onRefresh();
    if (!selected) return;
    const row = selected.type === "kb" ? await onLoad(selected.data.id) : await onLoadNoteSet(selected.data.id);
    setDetail(row);
  }

  async function withBusy(task: () => Promise<void>, reload = true, actionKey = "knowledge-busy") {
    await runAction(actionKey, async () => {
      setBusy(true);
      try {
        await task();
        if (reload) await reloadSelected();
      } finally {
        setBusy(false);
      }
    });
  }

  function updateQaRow(index: number, patch: Partial<KnowledgeQaItem>) {
    setQaRows((rows) => rows.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)));
  }

  function addQaRow() {
    setQaRows((rows) => [...rows, { _localId: `qa-new-${Date.now()}-${Math.random()}`, question: "", reply: "" }]);
  }

  function deleteQaRow(index: number) {
    setQaRows((rows) => rows.filter((_, rowIndex) => rowIndex !== index));
  }

  async function readImageFile(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error || new Error("failed to read image"));
      reader.readAsDataURL(file);
    });
  }

  async function chooseQaImage(index: number, file?: File) {
    if (!file) return;
    const imageBase64 = await readImageFile(file);
    updateQaRow(index, {
      image_base64: imageBase64,
      image_name: file.name,
      image_content_type: file.type,
    });
  }

  function validateQaRows() {
    for (let index = 0; index < qaRows.length; index += 1) {
      const row = qaRows[index];
      if (!row.question.trim()) return `第 ${index + 1} 行问题不能为空`;
      if (!String(row.reply || "").trim() && !row.image_base64) return `第 ${index + 1} 行回复和图片至少填写一项`;
    }
    return "";
  }

  async function saveQaRows() {
    if (!selected || selected.type !== "kb") return;
    const message = validateQaRows();
    if (message) {
      alert(message);
      return;
    }
    await withBusy(() => onSaveQaItems(selected.data.id, qaRows.map(({ _localId, ...row }) => ({
      question: row.question.trim(),
      reply: String(row.reply || "").trim(),
      image_base64: row.image_base64 || "",
      image_name: row.image_name || "",
      image_content_type: row.image_content_type || "",
    }))));
  }

  function toggleCreateShop(shopId: number, checked: boolean) {
    setCreateShopIds((ids) => (checked ? [...ids, shopId] : ids.filter((id) => id !== shopId)));
  }

  async function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim()) return;
    await withBusy(async () => {
      const body = { name: name.trim(), description: description.trim(), shop_ids: createShopIds };
      if (createType === "kb") await onCreate(body);
      else await onCreateNoteSet(body);
      setName("");
      setDescription("");
      setCreateShopIds([]);
    });
  }

  return (
    <div className="page knowledge-page">
      <div className="page-header">
        <div className="page-header-left">
          <BookOpen size={22} />
          <div>
            <h2 className="page-title">知识库</h2>
            <p className="page-subtitle">{knowledgeBases.length} 个知识库 · {noteSets.length} 个注意事项，用于约束客服和 AI 回复</p>
          </div>
        </div>
      </div>

      <div className="knowledge-layout">
        <section className="knowledge-sidebar">
          <div className="knowledge-sidebar-header"><BookOpen size={18} /> 内容列表</div>
          <div className="knowledge-list">
            {mixedList.map((item) => (
              <button key={itemKey(item)} className={`knowledge-item ${selected && itemKey(selected) === itemKey(item) ? "active" : ""}`} onClick={() => setSelectedKey(itemKey(item))}>
                <div className="knowledge-item-name">
                  {item.type === "kb" ? <BookOpen size={14} /> : <FileText size={14} />}
                  <strong>{item.data.name}</strong>
                  <span className={`knowledge-item-type-badge ${item.type}`}>{item.type === "kb" ? "知识库" : "注意事项"}</span>
                </div>
                <span>{item.data.description || "暂无说明"}</span>
                <small>
                  {item.type === "kb" ? `${item.data.file_count || 0} 个文件` : `${item.data.item_count || 0} 条内容`}
                  {" · "}{item.data.shop_count || 0} 个店铺 · <StatusBadge status={item.data.status} />
                </small>
              </button>
            ))}
            {!mixedList.length && <div className="empty-note">暂无知识库或注意事项</div>}
          </div>
        </section>

        <section className="knowledge-main">
          <div className="knowledge-main-header">
            {isKnowledgeBase ? <BookOpen size={18} /> : <FileText size={18} />}
            <span>{selected?.data.name || "选择内容"}</span>
          </div>
          {selected ? (
            <>
              <div className="kb-toolbar">
                <div>
                  <strong>{detail?.name || selected.data.name}</strong>
                  <p>{detail?.description || selected.data.description || (isKnowledgeBase ? "用于文档和结构化问答检索" : "用于约束客服回复时必须注意的事项")}</p>
                </div>
                <div className="kb-status-actions">
                  <StatusBadge status={status} />
                  {canManage && (
                    <>
                      <button className="btn btn-sm" disabled={busy} onClick={() => withBusy(() => (
                        isKnowledgeBase
                          ? onUpdate(selected.data.id, { status: status === "active" ? "disabled" : "active" })
                          : onUpdateNoteSet(selected.data.id, { status: status === "active" ? "disabled" : "active" })
                      ))}>{status === "active" ? "停用" : "启用"}</button>
                      <button className="btn btn-sm btn-danger-text" disabled={busy} onClick={() => {
                        if (!confirm(`确定删除「${selected.data.name}」吗？`)) return;
                        withBusy(async () => {
                          if (isKnowledgeBase) await onDelete(selected.data.id);
                          else await onDeleteNoteSet(selected.data.id);
                          setSelectedKey("");
                          setDetail(null);
                        }, false).then(onRefresh);
                      }}>删除</button>
                    </>
                  )}
                </div>
              </div>

              <div className="kb-bindings">
                <span className="kb-bindings-label">绑定店铺：</span>
                {shops.map((shop) => (
                  <label key={shop.id} className="kb-shop-check">
                    <input
                      type="checkbox"
                      disabled={!canManage || busy}
                      checked={shopIds.includes(shop.id)}
                      onChange={(event) => setShopIds(event.target.checked ? [...shopIds, shop.id] : shopIds.filter((id) => id !== shop.id))}
                    />
                    {shop.name}
                  </label>
                ))}
                {canManage && (
                  <button className="btn btn-sm" disabled={busy || !detail} onClick={() => withBusy(() => (
                    isKnowledgeBase
                      ? onUpdate(selected.data.id, { shop_ids: shopIds })
                      : onUpdateNoteSet(selected.data.id, { shop_ids: shopIds })
                  ))}>保存绑定</button>
                )}
              </div>

              {isKnowledgeBase ? (
                <>
                  <div className="qa-editor">
                    <div className="qa-editor-header">
                      <div>
                        <strong>结构化问答</strong>
                        <span>问题必填；回复和图片至少填写一项。命中后优先使用这里的回复和图片。</span>
                      </div>
                      {canManage && (
                        <div className="qa-editor-actions">
                          <button className="btn btn-sm" type="button" disabled={busy} onClick={addQaRow}><Plus size={14} /> 添加</button>
                          <button className="btn btn-primary btn-sm" type="button" disabled={busy} onClick={saveQaRows}>
                            {busy ? <Loader2 className="spin" size={14} /> : <Save size={14} />}
                            {busy ? "保存中" : "保存"}
                          </button>
                        </div>
                      )}
                    </div>
                    <div className="qa-table-wrap">
                      <table className="qa-table">
                        <thead>
                          <tr>
                            <th>问题</th>
                            <th>回复</th>
                            <th>图片</th>
                            {canManage && <th>操作</th>}
                          </tr>
                        </thead>
                        <tbody>
                          {qaRows.map((row, index) => (
                            <tr key={row._localId}>
                              <td>
                                <textarea className="input input-textarea qa-input" disabled={!canManage || busy} value={row.question} onChange={(event) => updateQaRow(index, { question: event.target.value })} />
                              </td>
                              <td>
                                <textarea className="input input-textarea qa-input" disabled={!canManage || busy} value={row.reply || ""} onChange={(event) => updateQaRow(index, { reply: event.target.value })} />
                              </td>
                              <td>
                                <div className="qa-image-cell">
                                  {row.image_base64 ? (
                                    <div className="qa-image-preview">
                                      <img src={row.image_base64} alt={row.image_name || "问答图片"} />
                                      {canManage && (
                                        <button className="btn btn-ghost btn-icon" type="button" disabled={busy} title="移除图片" onClick={() => updateQaRow(index, { image_base64: "", image_name: "", image_content_type: "" })}>
                                          <X size={14} />
                                        </button>
                                      )}
                                    </div>
                                  ) : (
                                    <span className="qa-image-empty"><ImageIcon size={16} /> 未上传</span>
                                  )}
                                  {canManage && (
                                    <label className="btn btn-sm qa-image-button">
                                      选择图片
                                      <input type="file" accept="image/*" disabled={busy} onChange={(event) => {
                                        const file = event.target.files?.[0];
                                        event.currentTarget.value = "";
                                        chooseQaImage(index, file).catch((error) => alert(String(error)));
                                      }} />
                                    </label>
                                  )}
                                </div>
                              </td>
                              {canManage && (
                                <td>
                                  <button className="btn btn-ghost btn-icon" type="button" disabled={busy} title="删除" onClick={() => deleteQaRow(index)}>
                                    <Trash2 size={15} />
                                  </button>
                                </td>
                              )}
                            </tr>
                          ))}
                          {!qaRows.length && (
                            <tr>
                              <td colSpan={canManage ? 4 : 3}><div className="empty-note">暂无结构化问答</div></td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {canManage && (
                    <label className="upload-drop">
                      <UploadCloud size={20} />
                      <span>{busy ? "处理中" : canUploadKnowledgeFile ? "上传 txt、md、csv、xlsx、docx、pdf，单文件不超过 20MB" : "文件数量已达上限"}</span>
                      <input type="file" accept=".txt,.md,.csv,.xlsx,.docx,.pdf" disabled={busy || !canUploadKnowledgeFile} onChange={(event) => {
                        const file = event.target.files?.[0];
                        event.currentTarget.value = "";
                        if (!file) return;
                        if (user.role !== "admin" && file.size > MAX_SERVICE_FILE_BYTES) {
                          alert("文件大小不能超过 20MB");
                          return;
                        }
                        if (!canUploadKnowledgeFile) {
                          alert("单个知识库最多上传 3 个文件");
                          return;
                        }
                        withBusy(() => onUpload(selected.data.id, file));
                      }} />
                    </label>
                  )}
                  {canManage && !canUploadKnowledgeFile && <div className="empty-note">单个知识库最多上传 3 个文件</div>}
                  <div className="file-list">
                    {((detail as KnowledgeBase)?.files || []).map((file) => (
                      <div className={`file-row ${file.status}`} key={file.id}>
                        <FileText size={16} /><strong>{file.filename}</strong>
                        <span className="file-status">{file.status}</span>
                        <small>{file.error || formatBytes(file.file_size || 0)}</small>
                      </div>
                    ))}
                    {!((detail as KnowledgeBase)?.files || []).length && <div className="empty-note">还没有上传文件</div>}
                  </div>
                </>
              ) : (
                <div className="qa-editor">
                  <div className="qa-editor-header">
                    <div>
                      <strong>注意事项</strong>
                      <span>这些内容会随店铺知识一起进入大模型上下文，用于约束客服回复。</span>
                    </div>
                  </div>
                  <div className="note-set-items">
                    {((detail as NoteSet)?.items || []).map((item: NoteSetItem, index: number) => (
                      <div className="note-item" key={item.id}>
                        <div className="note-index">{index + 1}</div>
                        {editingNoteId === item.id ? (
                          <div className="note-edit-row">
                            <textarea className="input input-textarea" value={editingNoteContent} disabled={busy} onChange={(event) => setEditingNoteContent(event.target.value)} />
                            <div className="note-edit-actions">
                              <button className="btn btn-sm" type="button" disabled={busy} onClick={() => { setEditingNoteId(null); setEditingNoteContent(""); }}>取消</button>
                              <button className="btn btn-primary btn-sm" type="button" disabled={busy || !editingNoteContent.trim()} onClick={() => withBusy(async () => {
                                await onUpdateNoteItem(selected.data.id, item.id, editingNoteContent);
                                setEditingNoteId(null);
                                setEditingNoteContent("");
                              })}>
                                {busy ? <Loader2 className="spin" size={14} /> : null}
                                {busy ? "保存中" : "保存"}
                              </button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <div className="note-content">{item.content}</div>
                            {canManage && (
                              <div className="note-actions">
                                <button className="btn btn-ghost btn-icon" type="button" title="编辑" disabled={busy} onClick={() => { setEditingNoteId(item.id); setEditingNoteContent(item.content); }}>
                                  <Edit3 size={15} />
                                </button>
                                <button className="btn btn-ghost btn-icon" type="button" title="删除" disabled={busy} onClick={() => {
                                  if (confirm("确定删除这条注意事项吗？")) withBusy(() => onDeleteNoteItem(selected.data.id, item.id));
                                }}>
                                  <Trash2 size={15} />
                                </button>
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    ))}
                    {!((detail as NoteSet)?.items || []).length && <div className="empty-note">暂无注意事项</div>}
                    {canManage && (
                      <div className="note-create-form">
                        <textarea className="input input-textarea" value={newNoteContent} disabled={busy} onChange={(event) => setNewNoteContent(event.target.value)} placeholder="新增一条注意事项" />
                        <button className="btn btn-primary btn-sm" type="button" disabled={busy || !newNoteContent.trim()} onClick={() => withBusy(async () => {
                          await onAddNoteItem(selected.data.id, newNoteContent);
                          setNewNoteContent("");
                        })}>
                          {busy ? <Loader2 className="spin" size={14} /> : <Plus size={14} />}
                          {busy ? "添加中" : "添加注意事项"}
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="empty-note">创建知识库或注意事项后即可开始维护内容</div>
          )}
        </section>

        {canManage && (
          <section className="knowledge-create">
            <div className="knowledge-create-header"><Plus size={18} /> 新建内容</div>
            <form onSubmit={submitCreate}>
              <div className="knowledge-create-type">
                <button className={`segmented-btn ${createType === "kb" ? "active" : ""}`} type="button" onClick={() => setCreateType("kb")}><BookOpen size={14} /> 知识库</button>
                <button className={`segmented-btn ${createType === "ns" ? "active" : ""}`} type="button" onClick={() => setCreateType("ns")}><FileText size={14} /> 注意事项</button>
              </div>
              <div className="form-group">
                <input className="input" value={name} onChange={(event) => setName(event.target.value)} placeholder={createType === "kb" ? "知识库名称" : "注意事项名称"} />
              </div>
              <div className="form-group">
                <textarea className="input input-textarea" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="说明" />
              </div>
              <div className="check-list">
                {shops.map((shop) => (
                  <label key={shop.id} className="kb-shop-check">
                    <input type="checkbox" checked={createShopIds.includes(shop.id)} onChange={(event) => toggleCreateShop(shop.id, event.target.checked)} />
                    {shop.name}
                  </label>
                ))}
                {!shops.length && <div className="empty-note">暂无可绑定店铺</div>}
              </div>
              {createType === "kb" && !canCreateKnowledgeBase && (
                <div className="empty-note">知识库数量已达上限：{knowledgeBases.length}/{maxKnowledgeBases}</div>
              )}
              <button className="btn btn-primary btn-full" disabled={!name.trim() || busy || (createType === "kb" && !canCreateKnowledgeBase)}>
                {busy ? <Loader2 className="spin" size={16} /> : null}
                {busy ? "创建中" : "创建"}
              </button>
            </form>
          </section>
        )}
      </div>
    </div>
  );
}
