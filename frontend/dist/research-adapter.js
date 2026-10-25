export function createResearchAdapter(client) {
  return {
    mode: client?.mode || 'live',
    connected: client?.connected === true,
    async listExperiments({ cursor = null, limit = 40, signal } = {}) {
      if (!client?.connected) throw new Error('Research service not connected.');
      return client.read('/api/research/experiments', { cursor, limit }, signal);
    },
    async listRecords({ experimentId, cursor = null, afterSequence = null, limit = 8, signal } = {}) {
      if (!client?.connected) throw new Error('Research service not connected.');
      return client.read('/api/research/experiments/' + encodeURIComponent(experimentId) + '/records',
        { cursor, after_sequence: afterSequence, limit }, signal);
    },
  };
}
