import { ImageResponse } from 'next/og';

/** The home-screen icon: the BlindSpot mark (a satellite's footprint with a blind notch), as icon.svg. */
export const size = { width: 180, height: 180 };
export const contentType = 'image/png';

export default function AppleIcon() {
  return new ImageResponse(
    (
      <div style={{ width: '100%', height: '100%', display: 'flex', background: '#0c0a08' }}>
        <svg width="180" height="180" viewBox="0 0 20 20">
          <circle cx="10" cy="10" r="6" fill="none" stroke="#c9a45a" strokeWidth="1.8" />
          <circle cx="13.2" cy="6.8" r="3.2" fill="#0c0a08" />
          <circle cx="10" cy="10" r="1.8" fill="#c9a45a" />
        </svg>
      </div>
    ),
    size,
  );
}
