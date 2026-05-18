{{/* Common labels */}}
{{- define "dataviz.labels" -}}
app.kubernetes.io/part-of: dataviz
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Image ref for a service. Usage: include "dataviz.image" (dict "ctx" $ "name" "viz-service") */}}
{{- define "dataviz.image" -}}
{{- $img := .ctx.Values.image -}}
{{- printf "%s/%s/%s:%s" $img.registry $img.org .name (toString $img.tag) -}}
{{- end -}}

{{/* wait-for-controlplane initContainer (single list item) */}}
{{- define "dataviz.waitForControlplane" -}}
- name: wait-for-controlplane
  image: {{ .Values.ordering.image | quote }}
  command:
    - /bin/sh
    - -c
    - "until curl -sf http://aggregation-controlplane:8091/health; do echo waiting for controlplane; sleep 3; done"
{{- end -}}

{{/* Name of the Secret to reference (existingSecret wins) */}}
{{- define "dataviz.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
dataviz-secrets
{{- end -}}
{{- end -}}
