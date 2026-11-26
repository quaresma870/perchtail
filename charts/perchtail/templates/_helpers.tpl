{{/* The release's name, cut to the 63 characters a label value allows. */}}
{{- define "perchtail.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "perchtail.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "perchtail.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "perchtail.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* registry/repository:tag, or @digest when one is given. */}}
{{- define "perchtail.image" -}}
{{- $repository := printf "%s/%s" .Values.image.registry .Values.image.repository | trimPrefix "/" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" $repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

{{/* The Secret and key holding CREDENTIAL_ENCRYPTION_KEY; fails without one. */}}
{{- define "perchtail.keySecretName" -}}
{{- if .Values.credentialEncryptionKey.existingSecret -}}
{{- .Values.credentialEncryptionKey.existingSecret -}}
{{- else if .Values.credentialEncryptionKey.value -}}
{{- printf "%s-key" (include "perchtail.fullname" .) -}}
{{- else -}}
{{- fail `
PerchTail needs CREDENTIAL_ENCRYPTION_KEY, and the chart never makes one up.
Create a Secret with a key of your own, keep a copy of it somewhere safe, and
point the chart at it:

  kubectl create secret generic perchtail-key \
    --from-literal=credential-encryption-key="$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")"

  helm install perchtail charts/perchtail \
    --set credentialEncryptionKey.existingSecret=perchtail-key
` -}}
{{- end -}}
{{- end -}}

{{- define "perchtail.keySecretKey" -}}
{{- if .Values.credentialEncryptionKey.existingSecret -}}
{{- .Values.credentialEncryptionKey.existingSecretKey -}}
{{- else -}}
credential-encryption-key
{{- end -}}
{{- end -}}

{{- define "perchtail.claimName" -}}
{{- .Values.persistence.existingClaim | default (printf "%s-data" (include "perchtail.fullname" .)) -}}
{{- end -}}

{{/* The command of the image's own HEALTHCHECK, for the exec probes. */}}
{{- define "perchtail.probe" -}}
exec:
  command:
    - python
    - -c
    - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000{{ . }}', timeout=4)"
{{- end -}}
