import Link from 'next/link';

/**
 * A case id the API does not know (404 "no such scar") or one that is not a number. Rendered inside
 * the case page's frame, so the bar and the way back stay.
 */
export default function CaseNotFound() {
  return (
    <div className="title-row not-found">
      <div className="title-row__text">
        <h1>No case with this number</h1>
        <p className="finding">
          The BlindSpot API has no burn scar with this number. The daily Sentinel-2 run re-measures the newest passes and stores their fields under new numbers, so a link
          made before a run can name a number that is gone. Open the field on the live map to read its current file.
        </p>
        <p className="not-found__links">
          <Link className="button-primary" href="/live">
            Open the live map
          </Link>
          <Link className="text-button" href="/method">
            How scars are found
          </Link>
        </p>
      </div>
    </div>
  );
}
