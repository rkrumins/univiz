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

{{/* Name of the Secret to reference (existingSecret wins) */}}
{{- define "dataviz.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
dataviz-secrets
{{- end -}}
{{- end -}}
