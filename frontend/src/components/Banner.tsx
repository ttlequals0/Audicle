import type { ReactNode } from "react";

export default function Banner({
  tone,
  pageWidth,
  children,
  action,
}: {
  tone: string;
  pageWidth: string;
  children: ReactNode;
  action: ReactNode;
}) {
  return (
    <div role="status" className={`border-b ${tone}`}>
      <div className={`${pageWidth} mx-auto px-4 py-2 flex items-center justify-between gap-3`}>
        <span className="text-sm">{children}</span>
        {action}
      </div>
    </div>
  );
}
