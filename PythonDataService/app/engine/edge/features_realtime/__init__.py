"""Real-time features for the Edge feature.

Hard rule: every column produced uses .shift(N) with N >= 0 only.
Never .shift(-N). Never imports from app.engine.edge.labels_oracle.
"""
