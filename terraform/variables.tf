variable "aws_region" {
  type    = string
  default = "af-south-1"
}

variable "app_name" {
  type    = string
  default = "solara-etl"
}

variable "container_image" {
  type = string
}

variable "task_cpu" {
  type    = string
  default = "512"
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "schedule_expression" {
  type    = string
  default = "cron(0 */2 * * ? *)"
}

variable "db_host" {
  type = string
}

variable "db_user" {
  type = string
}

variable "db_name" {
  type    = string
  default = "solara"
}

variable "slack_channel_id" {
  type = string
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "slack_bot_token" {
  type      = string
  sensitive = true
}
