import { Gauge, Store, UserRound } from "lucide-react";
import type { Quota, Shop, User } from "../types/types";
import { statusText } from "../utils/helpers";

export function ProfilePage({ user, quota, shops }: {
  user: User;
  quota: Quota | null;
  shops: Shop[];
}) {
  const total = quota?.max_llm_replies ?? 0;
  const used = quota?.llm_reply_count ?? 0;
  const remaining = quota?.remaining_llm_replies ?? 0;
  const percent = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;

  return (
    <div className="page profile-page">
      <div className="page-header">
        <div className="page-header-left">
          <UserRound size={22} />
          <div>
            <h2 className="page-title">个人中心</h2>
            <p className="page-subtitle">{user.display_name} · {user.role === "admin" ? "管理员" : "客服"}</p>
          </div>
        </div>
      </div>

      <div className="profile-layout">
        <section className="profile-card profile-quota-card">
          <div className="profile-card-header">
            <Gauge size={18} />
            <span>共享大模型额度</span>
          </div>
          <div className="quota-number">{remaining}</div>
          <div className="quota-caption">剩余额度</div>
          <div className="quota-progress" aria-label="大模型额度使用进度">
            <span style={{ width: `${percent}%` }} />
          </div>
          <div className="profile-stats">
            <div>
              <strong>{total}</strong>
              <span>总额度</span>
            </div>
            <div>
              <strong>{used}</strong>
              <span>已使用</span>
            </div>
          </div>
        </section>

        <section className="profile-card">
          <div className="profile-card-header">
            <Store size={18} />
            <span>我的店铺</span>
          </div>
          <div className="quota-number">{shops.length}</div>
          <div className="quota-caption">当前可管理店铺</div>
          <div className="profile-shop-list">
            {shops.slice(0, 6).map((shop) => (
              <div key={shop.id} className="profile-shop-row">
                <strong>{shop.name}</strong>
                <span>{statusText(shop.status)}</span>
              </div>
            ))}
            {!shops.length && <div className="empty-note">暂无店铺</div>}
          </div>
        </section>

        <section className="profile-card profile-account-card">
          <div className="profile-card-header">
            <UserRound size={18} />
            <span>账号信息</span>
          </div>
          <div className="profile-info-row">
            <span>用户名</span>
            <strong>{user.username}</strong>
          </div>
          <div className="profile-info-row">
            <span>显示名</span>
            <strong>{user.display_name}</strong>
          </div>
          <div className="profile-info-row">
            <span>角色</span>
            <strong>{user.role === "admin" ? "管理员" : "客服"}</strong>
          </div>
        </section>
      </div>
    </div>
  );
}
