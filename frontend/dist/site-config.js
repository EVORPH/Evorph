// Public outbound links. Set the real URLs here when they are ready.
// Leave a value empty to keep that footer entry visible but disabled.
export const socialLinks = {
  github: 'https://github.com/EVORPH',
  x: 'https://x.com/EvorphRobinhood',
};

// Seconds between automatic archive reads in the research view.
// 0 leaves the archive read-only; any live value must be set here on purpose
// and stays between 15 and 3600. These reads are archive GETs only; they never
// start an experiment or a model call.
export const researchAutoUpdateSeconds = 60;
