def apply_overrides(conf_gate, score_gate, *, markov_state='', trades_last_hour=0):
    # Apply constraints to adjust the scores and confidence gates
    if abs(score_gate - 1.0) > 4.0:
        score_gate = max(0.9, min(1.1, score_gate))
    
    if abs(conf_gate - 0.5) > 0.08:
        conf_gate = max(0.4, min(0.6, conf_gate))
    
    return conf_gate, score_gate

# Example usage
conf_gate, score_gate = apply_overrides(0.7, 0.9)
print(f"Adjusted Confidence Gate: {conf_gate}")
print(f"Adjusted Score Gate: {score_gate}")