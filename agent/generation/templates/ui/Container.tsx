import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/** Centered page-width wrapper using the site's measured content width (max-w-site). */
export function Container({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("mx-auto w-full max-w-site px-4 lg:px-8", className)}>{children}</div>;
}
