import { useAppUpdate } from "../lib/appUpdate";
import { useAnyUnsavedChanges } from "../lib/useUnsavedChanges";
import Banner from "./Banner";

export default function UpdateBanner({ pageWidth }: { pageWidth: string }) {
  const update = useAppUpdate();
  const dirty = useAnyUnsavedChanges();

  if (!update) return null;

  return (
    <Banner
      tone="border-accent/50 bg-accent/10 text-fg"
      pageWidth={pageWidth}
      action={
        <button className="btn-ghost" disabled={dirty} onClick={update}>
          Reload to update
        </button>
      }
    >
      {dirty ? "Update ready. Save your changes before reloading." : "An app update is ready."}
    </Banner>
  );
}
