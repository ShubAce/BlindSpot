import Link from 'next/link';

/**
 * The error state of a whole app screen whose one read failed on the server: the title stays, one
 * sentence says what failed and why (ApiError.userMessage via serverGet), and Retry reloads the page.
 * A plain link, so it works before any JavaScript has loaded. data-load-failed: the offline worker
 * never lets this page replace a good copy it saved (components/pwa/LoadFailed.tsx).
 */
export function ScreenError({ title, what, message, href = '' }: { title: string; what: string; message: string; href?: string }) {
  return (
    <div className="title-row">
      <div className="title-row__text">
        <h1>{title}</h1>
        <p className="finding" role="status" data-load-failed="">
          {`Couldn't load ${what}. ${message}`}
        </p>
        <p className="not-found__links">
          <a className="button-primary" href={href || '?'}>
            Retry
          </a>
          <Link className="text-button" href="/live">
            Open the live map
          </Link>
        </p>
      </div>
    </div>
  );
}
