import { type JSX } from "react";

export function NavButton({ active, icon, label, onClick }: {
  active: boolean;
  icon: JSX.Element;
  label: string;
  onClick: () => void;
}) {
  return (
    <button className={`nav-button ${active ? "active" : ""}`} onClick={onClick}>
      {icon}
      <span>{label}</span>
    </button>
  );
}
