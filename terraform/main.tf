terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  
  backend "s3" {
    bucket         = "solara-etl-terraform-state"
    key            = "fargate/terraform.tfstate"
    region         = "af-south-1"
    encrypt        = true
    dynamodb_table = "terraform-locks"
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_cloudwatch_log_group" "solara_etl" {
  name = "/aws/ssm/solara-etl"
}

resource "aws_ssm_parameter" "db_password" {
  name      = "/solara-etl/db-password"
  type      = "SecureString"
  value     = var.db_password
  overwrite = true

  tags = {
    Name = var.app_name
  }
}

resource "aws_ssm_parameter" "slack_bot_token" {
  name      = "/solara-etl/slack-bot-token"
  type      = "SecureString"
  value     = var.slack_bot_token
  overwrite = true

  tags = {
    Name = var.app_name
  }
}

resource "aws_ecs_cluster" "solara_etl" {
  name = "${var.app_name}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name = var.app_name
  }
}

resource "aws_ecs_cluster_capacity_providers" "solara_etl" {
  cluster_name       = aws_ecs_cluster.solara_etl.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    base              = 1
    weight            = 100
    capacity_provider = "FARGATE"
  }
}

resource "aws_iam_role" "ecs_task_role" {
  name = "${var.app_name}-task-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "ecs_task_policy" {
  name = "${var.app_name}-task-policy"
  role = aws_iam_role.ecs_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = "${data.aws_cloudwatch_log_group.solara_etl.arn}:*"
      },
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters"
        ]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/solara-etl/*",
          "arn:aws:ssm:us-east-1:${data.aws_caller_identity.current.account_id}:parameter/snowflake/*",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/odoo_etl/*"
        ]
      }
    ]
  })
}

resource "aws_security_group" "ecs_tasks" {
  name   = "${var.app_name}-sg"
  vpc_id = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = var.app_name
  }
}

resource "aws_ecs_task_definition" "solara_etl" {
  family                   = var.app_name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([{
    name      = var.app_name
    image     = var.container_image
    essential = true

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = data.aws_cloudwatch_log_group.solara_etl.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    environment = [
      { name = "DB_HOST", value = var.db_host },
      { name = "DB_PORT", value = "5432" },
      { name = "DB_NAME", value = var.db_name },
      { name = "DB_USER", value = var.db_user },
      { name = "SLACK_CHANNEL_ID", value = var.slack_channel_id },
      { name = "CLOUDWATCH_LOG_GROUP", value = data.aws_cloudwatch_log_group.solara_etl.name },
      { name = "AWS_REGION", value = var.aws_region }
    ]

    secrets = [
      {
        name      = "DB_PASSWORD"
        valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/solara-etl/db-password"
      },
      {
        name      = "SLACK_BOT_TOKEN"
        valueFrom = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/solara-etl/slack-bot-token"
      }
    ]
  }])
}

resource "aws_cloudwatch_event_rule" "solara_etl" {
  name                = "${var.app_name}-schedule"
  schedule_expression = var.schedule_expression

  tags = {
    Name = var.app_name
  }
}

resource "aws_iam_role" "eventbridge" {
  name = "${var.app_name}-eventbridge"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "events.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge" {
  name = "${var.app_name}-eventbridge"
  role = aws_iam_role.eventbridge.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        Resource = aws_ecs_task_definition.solara_etl.arn
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = aws_iam_role.ecs_task_role.arn
      }
    ]
  })
}

resource "aws_cloudwatch_event_target" "ecs" {
  rule     = aws_cloudwatch_event_rule.solara_etl.name
  arn      = aws_ecs_cluster.solara_etl.arn
  role_arn = aws_iam_role.eventbridge.arn

  ecs_target {
    launch_type             = "FARGATE"
    task_count              = 1
    task_definition_arn     = aws_ecs_task_definition.solara_etl.arn
    platform_version        = "LATEST"
    enable_ecs_managed_tags = true

    network_configuration {
      subnets          = data.aws_subnets.default.ids
      security_groups  = [aws_security_group.ecs_tasks.id]
      assign_public_ip = true
    }
  }
}
