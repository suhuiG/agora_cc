import Link from "next/link";
import { MyAccessClient } from "@/components/access/MyAccessClient";
import { Icon } from "@/components/ui/icon";

export default function GovernancePage() {
  return (
    <div>
      <MyAccessClient />
      <div className="mx-auto mt-9 max-w-6xl border-t border-border pt-5">
        <Link
          href="/admin"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-sm font-medium text-blue-700 hover:text-blue-900"
        >
          <Icon name="shield" size={15} />
          관리자 콘솔
          <Icon name="external" size={13} />
        </Link>
      </div>
    </div>
  );
}
