output "cluster_name" {
  value = aws_ecs_cluster.solara_etl.name
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.solara_etl.arn
}

output "log_group_name" {
  value = data.aws_cloudwatch_log_group.solara_etl.name
}

output "eventbridge_rule" {
  value = aws_cloudwatch_event_rule.solara_etl.name
}

output "logs_command" {
  value = "aws logs tail ${data.aws_cloudwatch_log_group.solara_etl.name} --follow --region ${var.aws_region}"
}
