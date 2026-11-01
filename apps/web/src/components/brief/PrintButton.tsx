'use client';

/**
 * Prints the brief (brief.css lays it out on A4 paper, light, without the site's bars and buttons).
 * The one client part of the brief: printing needs the browser.
 */
export function PrintButton() {
  return (
    <button type="button" className="br-button" onClick={() => window.print()}>
      <svg aria-hidden="true" width="14" height="14" viewBox="0 0 14 14">
        <path
          d="M3.75 5.25V1.75h6.5v3.5M3.75 10.25h-1.5a.75.75 0 0 1-.75-.75V6a.75.75 0 0 1 .75-.75h9.5a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-.75.75h-1.5M3.75 8.25h6.5v4h-6.5z"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
          strokeLinejoin="round"
        />
      </svg>
      Print
    </button>
  );
}
