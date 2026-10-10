{{- define "demo-mcp.name" -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}

{{- define "demo-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ .Values.server }}-mcp
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "demo-mcp.labels" -}}
{{ include "demo-mcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
