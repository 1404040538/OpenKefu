export type User = {
  id: number;
  username: string;
  display_name: string;
  role: "admin" | "service";
  is_active: boolean;
  shop_ids?: number[];
};

export type Shop = {
  id: number;
  name: string;
  remark: string;
  mall_id?: string;
  nickname?: string;
  status: string;
  auto_reply_enabled: number | boolean;
  has_login_cache?: number | boolean;
  last_error?: string;
  transfer_csids?: string;
  greeting_message?: string;
  greeting_use_llm?: number | boolean;
  force_ai_reply?: number | boolean;
};

export type ShopNote = {
  id: number;
  shop_id: number;
  content: string;
  created_by?: number;
  created_at: string;
  updated_at: string;
};

export type TransferService = {
  csid: string;
  username?: string;
  nickname?: string;
};

export type Conversation = {
  id: number;
  shop_id: number;
  mall_id?: string;
  user_uid: string;
  nickname: string;
  shop_name?: string;
  shop_status?: string;
  shop_auto_reply_enabled?: number | boolean;
  bot_reply_enabled?: number | boolean;
  human_attention_required?: number | boolean;
  human_attention_reason?: string;
  human_attention_at?: string;
  last_message_preview: string;
  unread_count: number;
  updated_at?: string;
};

export type Message = {
  id: number;
  shop_id: number;
  conversation_id: number;
  direction: "inbound" | "outbound" | "system";
  kind: string;
  content?: string;
  goods_json?: string;
  size_json?: string;
  raw_json?: string;
  status: string;
  error?: string;
  created_at: string;
};

export type CustomerGoodsCard = {
  source?: string;
  goods_id: string;
  goods_name?: string;
  goods_thumb_url?: string;
  mall_link_url?: string;
  display_price?: string;
  title?: string;
};

export type CustomerOrderGoods = {
  goods_name?: string;
  spec?: string;
  quantity?: string | number;
  thumb_url?: string;
};

export type CustomerOrderCard = {
  order_sn?: string;
  status?: string;
  amount?: string;
  created_at?: string;
  after_sale_status?: string;
  shipping_status?: string;
  goods?: CustomerOrderGoods[];
};

export type CustomerContext = {
  conversation_id?: number;
  user_uid?: string;
  recent_goods: CustomerGoodsCard[];
  orders: CustomerOrderCard[];
  orders_status?: "ready" | "empty" | "unavailable" | "failed" | string;
  orders_message?: string;
};

export type RuntimeLog = {
  id: number | string;
  level: string;
  module?: string;
  action: string;
  message: string;
  mall_id?: string;
  shop_id?: number;
  conversation_id?: number;
  user_uid?: string;
  request_id?: string;
  error_trace?: string;
  created_at?: string;
  run_id?: string;
  pid?: number;
  context?: Record<string, any>;
};

export type LogFacet = {
  count: number;
  level?: string;
  module?: string;
  action?: string;
  run_id?: string;
  started_at?: string;
  ended_at?: string;
};

export type LogResponse = {
  items: RuntimeLog[];
  total: number;
  limit: number;
  offset: number;
  facets: {
    levels: LogFacet[];
    modules: LogFacet[];
    actions: LogFacet[];
    runs: LogFacet[];
  };
};


export type ReturnRecord = {
  id: number;
  shop_id: number;
  shop_name?: string;
  conversation_id?: number;
  message_id?: number;
  user_uid?: string;
  username: string;
  order_no: string;
  order_status: string;
  record_type: "return" | "exchange" | "address_change" | "refund_only" | "logistics_intercept" | "other" | string;
  new_address?: string;
  remark?: string;
  source_message?: string;
  slots?: Record<string, any>;
  order_snapshot?: Record<string, any>;
  created_at: string;
};

export type ReturnRecordResponse = {
  items: ReturnRecord[];
  total: number;
  limit: number;
  offset: number;
};

export type KnowledgeFile = {
  id: number;
  filename: string;
  status: string;
  error?: string;
  file_size?: number;
  created_at?: string;
};

export type KnowledgeQaItem = {
  id?: number;
  knowledge_base_id?: number;
  question: string;
  reply?: string;
  image_base64?: string;
  image_name?: string;
  image_content_type?: string;
  sort_order?: number;
  created_at?: string;
  updated_at?: string;
};

export type KnowledgeBase = {
  id: number;
  name: string;
  description: string;
  status: string;
  created_by?: number | null;
  shop_ids: number[];
  shop_count?: number;
  file_count?: number;
  files?: KnowledgeFile[];
  qa_items?: KnowledgeQaItem[];
};

export type NoteSetItem = {
  id: number;
  note_set_id: number;
  content: string;
  created_by?: number;
  created_at?: string;
  updated_at?: string;
};

export type NoteSet = {
  id: number;
  name: string;
  description: string;
  status: string;
  created_by?: number | null;
  shop_ids: number[];
  shop_count?: number;
  item_count?: number;
  items?: NoteSetItem[];
  created_at?: string;
  updated_at?: string;
};

export type ViewName = "shops" | "chat" | "knowledge" | "returnRecords" | "users" | "logs" | "profile" ;
export type AuthMode = "checking" | "setup" | "login" | "register";
export type ShopFilter = "all" | number;
