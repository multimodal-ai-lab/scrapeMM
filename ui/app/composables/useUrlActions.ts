/**
 * What can be done to a URL of a job, shared by the jobs overview and a job's page:
 * stop it while it is being retrieved (the job goes on with its other URLs), or
 * retrieve it again once it was -- without the cache, as a new job.
 */
export function useUrlActions() {
  const api = useApi()
  const toast = useToast()
  // The job and URL whose action is under way
  const acting = ref('')

  /** The action a URL offers, if any: `done` is whether it has a result already. */
  function urlAction(done: boolean, jobRunning: boolean) {
    if (!done) {
      return jobRunning
        ? { kind: 'stop' as const, icon: 'i-fa7-solid-stop', title: 'Stop retrieving this URL' }
        : null
    }
    return { kind: 'retry' as const, icon: 'i-fa7-solid-rotate-right',
             title: 'Retrieve this URL again, without the cache' }
  }

  /**
   * Does it. A retry starts a new job, which is easy to miss in a long list or on another
   * page: a toast says so and leads to it. Resolves to the new job's id for a retry.
   * Throws the server's complaint, for the page to show.
   */
  async function runUrlAction(jobId: string, url: string, kind: 'stop' | 'retry'): Promise<string | null> {
    acting.value = `${jobId}|${url}`
    try {
      if (kind === 'stop') {
        await api.post(`/v1/jobs/${jobId}/interrupt`, { url })
        return null
      }
      const started = await api.post<{ job_id: string }>(`/v1/jobs/${jobId}/retry`, { url })
      toast.add({
        title: 'Retrieving the URL again',
        description: url,
        icon: 'i-fa7-solid-rotate-right',
        actions: [{ label: 'Open the new job', color: 'neutral', variant: 'outline',
                    onClick: () => navigateTo(`/jobs/${started.job_id}`) }],
      })
      return started.job_id
    } finally {
      acting.value = ''
    }
  }

  return { acting, urlAction, runUrlAction }
}
