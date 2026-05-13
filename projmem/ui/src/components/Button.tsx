import { forwardRef, ButtonHTMLAttributes, ReactNode } from "react";

// Consistent button primitive used everywhere in the UI. Replaces a
// bunch of hand-rolled `<button className="…">` instances that drifted
// across components and themes. Pulls every interactive action into
// one place: focus ring, hover, active, disabled, loading.
//
// Variants:
//   primary    — accent fill (default, top-of-task actions)
//   secondary  — line + sunken hover (most secondary actions)
//   subtle     — text-only with hover bg (tertiary / chip-like)
//   ghost      — fully transparent until hover (icons in toolbars)
//   danger     — red fill (destructive: deny, delete, force-purge)
//   success    — green fill (approve)
//
// Sizes:
//   xs   24 px tall, [10px] font   chips, toolbar bits
//   sm   28 px tall, [11px] font   most inspector buttons
//   md   32 px tall, [12px] font   top bar / dialog primaries

export type ButtonVariant =
  | "primary" | "secondary" | "subtle" | "ghost" | "danger" | "success";
export type ButtonSize = "xs" | "sm" | "md";

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    "bg-accent text-accent-fg border-accent hover:opacity-90 " +
    "active:opacity-100 active:scale-[.98] focus-visible:ring-accent",
  secondary:
    "bg-elev text-ink border-line hover:bg-sunken " +
    "active:scale-[.98] focus-visible:ring-accent",
  subtle:
    "bg-transparent text-muted border-transparent hover:text-ink " +
    "hover:bg-sunken focus-visible:ring-accent",
  ghost:
    "bg-transparent text-muted border-transparent hover:text-ink " +
    "focus-visible:ring-accent",
  danger:
    "bg-bad text-white border-bad hover:opacity-90 " +
    "active:opacity-100 active:scale-[.98] focus-visible:ring-bad",
  success:
    "bg-good text-white border-good hover:opacity-90 " +
    "active:opacity-100 active:scale-[.98] focus-visible:ring-good",
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  xs: "h-6 px-1.5 text-[10px] rounded gap-1",
  sm: "h-7 px-2.5 text-[11px] rounded gap-1",
  md: "h-8 px-3 text-xs rounded-md gap-1.5",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?:    ButtonSize;
  loading?: boolean;
  // Pull `icon` to its own slot so the spinner replaces it cleanly.
  icon?:    ReactNode;
  // Optional right-side icon (e.g. ⓘ next to "ghosts").
  trailing?: ReactNode;
  fullWidth?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    { variant = "secondary", size = "sm", loading = false,
      icon, trailing, fullWidth = false,
      disabled, className = "", children, ...rest },
    ref,
  ) {
    const isDisabled = disabled || loading;
    return (
      <button
        ref={ref}
        disabled={isDisabled}
        aria-busy={loading || undefined}
        className={[
          "inline-flex items-center justify-center font-medium",
          "border transition-[transform,opacity,background-color,color] duration-100",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-offset-1",
          "focus-visible:ring-offset-bg",
          "disabled:opacity-50 disabled:cursor-not-allowed disabled:active:scale-100",
          SIZE_CLASSES[size],
          VARIANT_CLASSES[variant],
          fullWidth ? "w-full" : "",
          className,
        ].join(" ")}
        {...rest}
      >
        {loading ? <Spinner /> : icon}
        {children}
        {!loading && trailing}
      </button>
    );
  },
);

function Spinner() {
  // 12 px SVG spinner — matches the xs/sm icon slot.
  return (
    <svg
      className="animate-spin"
      width="12" height="12" viewBox="0 0 24 24"
      aria-hidden
    >
      <circle cx="12" cy="12" r="9" fill="none"
              stroke="currentColor" strokeWidth="3"
              strokeOpacity="0.25" />
      <path d="M21 12a9 9 0 0 1-9 9" fill="none"
            stroke="currentColor" strokeWidth="3"
            strokeLinecap="round" />
    </svg>
  );
}
