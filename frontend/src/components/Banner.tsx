import type { ReactNode } from "react";

export default function Banner({
  role,
  tone,
  pageWidth,
  children,
  action,
}: {
  role?: "status";
  tone: string;
  pageWidth: string;
  children: ReactNode;
  action: ReactNode;
}) {
  return (
    <div role={role} className={`border-b ${tone}`}>
      <div className={`${pageWidth} mx-auto px-4 py-2 flex items-center justify-between gap-3`}>
        <span className="text-sm">{children}</span>
        {action}
      </div>
    </div>
  );
}
