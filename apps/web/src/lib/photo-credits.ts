/**
 * The openly licensed photographs in public/photos, with the credit each licence asks for.
 *
 * Moved here from public/photos/credits.json (written by the taste skill's assets.mjs) so no data file
 * sits in public/. These are facts about pictures, not data: the landing page shows each photograph
 * with a caption that says what it is, and the footer prints `credit` small, as the licences require.
 * Every photograph here is CC BY-SA, so an altered version (recoloured, combined) must stay CC BY-SA;
 * the page crops them and changes nothing else.
 */

export type PhotoCredit = {
  /** Path under public/, as the <img> src. */
  src: string;
  width: number;
  height: number;
  title: string;
  creator: string;
  /** When the photograph was taken, as the source page states it (left out where it does not). */
  date?: string;
  license: string;
  licenseUrl: string;
  /** The source page (where the licence is stated). */
  page: string;
  via: string;
};

export const PHOTOS = {
  strawBurning: {
    src: '/photos/np-india-burning-48-6315309342.jpg',
    width: 1280,
    height: 850,
    title: 'NP India burning 48',
    creator: 'CIAT',
    date: '2011-11-01',
    license: 'CC BY-SA 2.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/2.0',
    page: 'https://commons.wikimedia.org/wiki/File:NP_India_burning_48_(6315309342).jpg',
    via: 'Wikimedia Commons',
  },
  rakingStubble: {
    src: '/photos/np-india-burning-46-6314792401.jpg',
    width: 1280,
    height: 850,
    title: 'NP India burning 46',
    creator: 'CIAT',
    date: '2011-11-01',
    license: 'CC BY-SA 2.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/2.0',
    page: 'https://commons.wikimedia.org/wiki/File:NP_India_burning_46_(6314792401).jpg',
    via: 'Wikimedia Commons',
  },
  seederTractor: {
    src: '/photos/agriculture-in-india-tractor-farming-punjab-preparing.jpg',
    width: 1280,
    height: 848,
    title: 'Agriculture in India: tractor farming, Punjab, preparing a field for a wheat crop without burning the previous crop stalk',
    creator: 'CIAT',
    date: '2011-11-02',
    license: 'CC BY-SA 2.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/2.0',
    page: 'https://commons.wikimedia.org/wiki/File:Agriculture_in_India_tractor_farming_Punjab_preparing_field_for_a_wheat_crop_without_burning_previous_crop_stalk.jpg',
    via: 'Wikimedia Commons',
  },
  happySeeder: {
    src: '/photos/national-agro-happy-seeder.jpg',
    width: 1280,
    height: 720,
    title: 'National Agro Happy Seeder',
    creator: 'FatherofGod',
    license: 'CC BY-SA 4.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/4.0',
    page: 'https://commons.wikimedia.org/w/index.php?curid=101089993',
    via: 'Wikimedia Commons',
  },
  delhiSmog: {
    src: '/photos/low-visibility-due-to-smog-at-new.jpg',
    width: 1920,
    height: 1312,
    title: 'Low visibility due to smog at New Delhi railway station, 31 Dec 2017, after 9 AM',
    creator: 'Sumita Roy Dutta',
    date: '2017-12-31',
    license: 'CC BY-SA 4.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/4.0',
    page: 'https://commons.wikimedia.org/wiki/File:Low_visibility_due_to_Smog_at_New_Delhi_Railway_station_31st_Dec_2017_after_9AM_DSCN8829_1.jpg',
    via: 'Wikimedia Commons',
  },
  gurgaonSmog: {
    src: '/photos/smog-as-visible-in-the-gurgaon-area.jpg',
    width: 1920,
    height: 1834,
    title: 'Smog as visible in the Gurgaon area near Delhi, Nov 2016',
    creator: 'Saurabh Kumar',
    date: '2016-11-08',
    license: 'CC BY-SA 4.0',
    licenseUrl: 'https://creativecommons.org/licenses/by-sa/4.0',
    page: 'https://commons.wikimedia.org/wiki/File:Smog_as_visible_in_the_Gurgaon_area_near_Delhi_on_Nov_2016.jpg',
    via: 'Wikimedia Commons',
  },
} as const satisfies Record<string, PhotoCredit>;

export type PhotoKey = keyof typeof PHOTOS;

/** "“NP India burning 48” by CIAT, CC BY-SA 2.0, via Wikimedia Commons" */
export function creditLine(p: PhotoCredit): string {
  return `“${p.title}” by ${p.creator}, ${p.license}, via ${p.via}`;
}
