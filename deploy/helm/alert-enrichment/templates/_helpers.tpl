{{- define "alert-enrichment.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "alert-enrichment.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "alert-enrichment.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "alert-enrichment.labels" -}}
helm.sh/chart: {{ include "alert-enrichment.chart" . }}
{{ include "alert-enrichment.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "alert-enrichment.selectorLabels" -}}
app.kubernetes.io/name: {{ include "alert-enrichment.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "alert-enrichment.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "alert-enrichment.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "alert-enrichment.secretName" -}}
{{- if .Values.slack.existingSecret -}}
{{- .Values.slack.existingSecret -}}
{{- else -}}
{{- include "alert-enrichment.fullname" . -}}
{{- end -}}
{{- end -}}
