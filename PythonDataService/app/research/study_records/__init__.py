"""The durable record of a finished study run (#3520, PRD #3490 R26).

Five studies used to keep nothing that outlives a day: Feature, Signal and Cross-sectional held their result in the job
store for 24 hours, and Indicator reliability and Realized vs IV answered the request and forgot. Each now leaves one
record per run in the research artifact store, next to the walk-forwards and Monte Carlos it is indexed beside.

* ``models``: the two files of a record.
* ``store``: where they live (``ArtifactStore``, one directory per study).
* ``specs``: what is particular to each study (its question, the instruments and window it covers, its headline).
* ``recorder``: the one call a job or a route makes after a result or a failure is produced.
"""
