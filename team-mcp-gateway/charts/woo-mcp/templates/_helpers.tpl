{{- define "woo-mcp.name" -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}-woo-mcp
{{- end -}}

{{- define "woo-mcp.selectorLabels" -}}
app.kubernetes.io/name: woo-mcp
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "woo-mcp.labels" -}}
{{ include "woo-mcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
