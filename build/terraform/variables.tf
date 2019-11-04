variable "region" {
  type = "string"
  default = "eu-west-2"
}

variable "bucket_prefix" {
  type = "string"
}

variable "runtime" {
  description = "runtime for lambda"
  default     = "python3.7"
}

variable "github_org" {
  description = "GitHub organisation short-name"
  type        = "string"
}

variable "aws_account_id" {
  default  = "779799343306"
}

variable "dependabot_lambda_zip_location" {
  default = "zipped_lambda_functions/cyber_dependabot_lambda.zip"
}

variable "dependabot_lambda_functionname" {
  default = "cyber_dependabot"
}

variable "dependabot_lambda_handler" {
  default  = "cyber_dependabot.lambda_handler"
}

variable "dependabot_lambda_memory" {
  default  = 128
}

variable "dependabot_lambda_timeout" {
  default  = 900
}

variable "dependabot_lambda_logarn" {
  default  = "arn:aws:logs:eu-west-2:*"
}
