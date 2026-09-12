import type { CSSProperties } from "react";
const paths: Record<string, string> = {
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  layers: "m12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5",
  play: "m8 4 13 8-13 8V4Z",
  spark: "m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3 3-7Z",
  code: "m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18",
  upload: "M12 16V3m-5 5 5-5 5 5M3 15v6h18v-6",
  down: "M12 3v13m-5-5 5 5 5-5M3 16v5h18v-5",
  arrow: "M4 12h16m-6-6 6 6-6 6",
  plus: "M12 4v16M4 12h16",
  close: "m6 6 12 12M6 18 18 6",
  check: "m4 12 5 5L20 6",
  branch: "M6 3v12a5 5 0 0 0 10 0V9M3 3h6M13 9h6M3 21h6",
  clock: "M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  box: "m3 7 9-5 9 5v10l-9 5-9-5V7Zm0 0 9 5 9-5M12 12v10",
  flow: "M3 3h6v6H3zM15 15h6v6h-6zM6 9v9h9M9 6h9v9",
};
export function Icon({
  name,
  size = 20,
  style,
}: {
  name: string;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      <path d={paths[name] || paths.box} />
    </svg>
  );
}
